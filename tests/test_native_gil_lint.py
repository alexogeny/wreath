from __future__ import annotations

from wreath._devtools.native_gil_lint import _uses_variable, scan_text


def codes(source: str) -> list[str]:
    return [finding.code for finding in scan_text("fixture.c", source)]


def test_python_api_inside_allow_threads_is_reported() -> None:
    assert "NG001" in codes("""
static void broken(PyObject *value) {
    Py_BEGIN_ALLOW_THREADS
    Py_INCREF(value);
    Py_END_ALLOW_THREADS
}
""")


def test_python_allocator_inside_allow_threads_is_reported() -> None:
    assert "NG001" in codes("""
static void broken(void) {
    Py_BEGIN_ALLOW_THREADS
    void *data = PyMem_Malloc(128);
    Py_END_ALLOW_THREADS
}
""")


def test_raw_allocator_inside_allow_threads_is_accepted() -> None:
    assert "NG001" not in codes("""
static void safe(void) {
    Py_BEGIN_ALLOW_THREADS
    void *data = PyMem_RawMalloc(128);
    Py_END_ALLOW_THREADS
}
""")


def test_blocking_io_while_holding_gil_is_reported() -> None:
    assert "NG002" in codes("""
static int broken(int fd, char *buffer) {
    return recv(fd, buffer, 1024, 0);
}
""")


def test_struct_member_call_named_like_a_syscall_is_not_blocking_io() -> None:
    assert "NG002" not in codes("""
static int fused(WreathTransportCAPI *capi, PyObject *transport, PyObject *data) {
    if (capi->write(transport, data) < 0) {
        return -1;
    }
    return capi->read(transport, data);
}
""")


def test_bare_syscall_still_reports_after_member_calls_are_excluded() -> None:
    assert "NG002" in codes("""
static int broken(int fd, const char *buffer) {
    return write(fd, buffer, 1024);
}
""")


def test_blocking_io_inside_allow_threads_is_accepted() -> None:
    assert "NG002" not in codes("""
static int safe(int fd, char *buffer) {
    int result;
    Py_BEGIN_ALLOW_THREADS
    result = recv(fd, buffer, 1024, 0);
    Py_END_ALLOW_THREADS
    return result;
}
""")


def test_borrowed_reference_crossing_gil_release_is_reported() -> None:
    assert "NG003" in codes("""
static int broken(PyObject *items) {
    PyObject *value = PyList_GetItem(items, 0);
    Py_BEGIN_ALLOW_THREADS
    do_work();
    Py_END_ALLOW_THREADS
    return PyObject_IsTrue(value);
}
""")


def test_borrowed_macro_reference_crossing_gil_release_is_reported() -> None:
    assert "NG003" in codes("""
static int broken(PyObject *items) {
    PyObject *value = PyList_GET_ITEM(items, 0);
    Py_BEGIN_ALLOW_THREADS
    do_work();
    Py_END_ALLOW_THREADS
    return PyObject_IsTrue(value);
}
""")


def test_owned_reference_can_cross_gil_release() -> None:
    assert "NG003" not in codes("""
static int safe(PyObject *items) {
    PyObject *value = Py_NewRef(PyList_GetItem(items, 0));
    Py_BEGIN_ALLOW_THREADS
    do_work();
    Py_END_ALLOW_THREADS
    int result = PyObject_IsTrue(value);
    Py_DECREF(value);
    return result;
}
""")


def test_unbalanced_gilstate_ensure_is_reported() -> None:
    assert "NG004" in codes("""
static void callback(void) {
    PyGILState_STATE state = PyGILState_Ensure();
    invoke_python();
}
""")


def test_gilstate_pair_is_accepted() -> None:
    assert "NG004" not in codes("""
static void callback(void) {
    PyGILState_STATE state = PyGILState_Ensure();
    invoke_python();
    PyGILState_Release(state);
}
""")


def test_python_api_in_pthread_callback_without_gil_is_reported() -> None:
    assert "NG005" in codes("""
static void *worker(void *arg) {
    PyObject_CallNoArgs((PyObject *)arg);
    return NULL;
}
static int start(PyObject *callable) {
    pthread_t thread;
    return pthread_create(&thread, NULL, worker, callable);
}
""")


def test_waiver_suppresses_one_intentional_blocking_call() -> None:
    assert "NG002" not in codes("""
static int intentional(int fd, char *buffer) {
    /* native-gil-lint: allow NG002 -- descriptor is nonblocking */
    return recv(fd, buffer, 1024, MSG_DONTWAIT);
}
""")


def test_borrowed_reference_member_and_dereference_uses_are_detected() -> None:
    assert _uses_variable("return value->ob_refcnt;", "value")
    assert _uses_variable("return *value != NULL;", "value")
    assert not _uses_variable("return other->ob_refcnt;", "value")


def test_diagnostics_preserve_default_and_specific_messages() -> None:
    blocking = scan_text(
        "fixture.c",
        "static int blocked(int fd) { return fsync(fd); }\n",
    )
    assert blocking[0].message == "potentially blocking native I/O while holding the GIL"

    imbalance = scan_text(
        "fixture.c",
        "static void callback(void) {\n    PyGILState_Release(state);\n}\n",
    )
    assert imbalance[0].line == 2
    assert imbalance[0].message == "callback has 0 Ensure and 1 Release call(s)"


def test_gil_state_does_not_cross_function_or_module_boundaries() -> None:
    findings = scan_text(
        "fixture.c",
        """
PyGILState_STATE module_state = PyGILState_Ensure();
static void first(void) {
    Py_BEGIN_ALLOW_THREADS
}
static void second(PyObject *value) {
    PyObject_CallNoArgs(value);
}
""",
    )

    assert [finding.code for finding in findings] == []


def test_python_api_outside_allow_threads_is_not_ng001() -> None:
    source = "static void safe(PyObject *value) { PyObject_CallNoArgs(value); }\n"

    assert "NG001" not in codes(source)


def test_borrowed_reference_is_checked_only_after_the_release_region() -> None:
    assert "NG003" not in codes("""
static void safe(PyObject *items) {
    PyObject *value = PyList_GetItem(items, 0);
    Py_BEGIN_ALLOW_THREADS
    PyObject_IsTrue(value);
    Py_END_ALLOW_THREADS
    unrelated();
}
""")


def test_only_registered_callbacks_without_an_ensure_report_ng005() -> None:
    ordinary = "static void ordinary(PyObject *value) { PyObject_CallNoArgs(value); }\n"
    acquired = """
static void *worker(void *arg) {
    PyGILState_STATE state = PyGILState_Ensure();
    PyObject_CallNoArgs((PyObject *)arg);
    PyGILState_Release(state);
    return NULL;
}
static int start(PyObject *callable) {
    pthread_t thread;
    return pthread_create(&thread, NULL, worker, callable);
}
"""
    no_python = """
static void *worker(void *arg) {
    consume(arg);
    return NULL;
}
static int start(void *value) {
    pthread_t thread;
    return pthread_create(&thread, NULL, worker, value);
}
"""

    assert "NG005" not in codes(ordinary)
    assert "NG005" not in codes(acquired)
    assert "NG005" not in codes(no_python)
