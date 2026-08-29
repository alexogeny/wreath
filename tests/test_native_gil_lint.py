from __future__ import annotations

from wreath._devtools.native_gil_lint import scan_text


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
