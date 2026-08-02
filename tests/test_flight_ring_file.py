"""The ring as a file, and what survives a process that dies badly.

The claim this whole feature rests on is one sentence: *the records are still
there after the process is gone*. That is not a claim a mock can support, so
the tests below start a fresh interpreter, let it publish into a mapped ring,
and then kill it in the two ways that actually happen -- a `SIGSEGV` from inside
its own work, and a `SIGKILL` from outside -- before decoding the file from the
parent. No atexit handler runs, no `msync` happens, nothing is flushed on the
way out.

The rest of the file covers the reader, which is written for the case where
something has already gone wrong and so has to report rather than raise: a
window clamped from a torn header, a slot whose bytes do not decode, a ring that
refused records because it was full.
"""

from __future__ import annotations

import os
import signal
import struct
import subprocess
import sys

import pytest

from wreath._flight_schema import (
    CELL_SIZE,
    RING_FILE_CURSOR_OFFSET,
    RING_FILE_HEADER_BYTES,
    EventKind,
    LogArg,
    LogCell,
    LossReason,
    RingFileHeader,
    Severity,
    ring_file_bytes,
)
from wreath.recording import read_ring_file

_flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)


@pytest.fixture
def ring_path(tmp_path):
    return str(tmp_path / "flight.wfrr")


def _recorder(path: str, records: int = 64):
    return _flight.Recorder(
        _flight.MODE_PULSE, ring_records=records, active_requests=32, ring_path=path
    )


def _serve(recorder, count: int = 1) -> None:
    for _ in range(count):
        request = recorder.begin(1, 1, 0)
        request.route(7, 3)
        request.finish(1_000, 200, 0, 0, 0, 12)


# --- the crash cases --------------------------------------------------------
#
# A subprocess rather than `os.fork`: xdist's worker has live threads, and
# CPython 3.14 correctly warns that running Python after forking such a process
# can deadlock on a lock a vanished thread held. The child uses this exact
# interpreter and imports this checkout, so it still exercises the extension
# that was just built without inheriting pytest's threads or captured streams.


def _silence_crash_reporting() -> None:
    """Stop the child printing a stack trace when we deliberately crash it.

    pytest enables `faulthandler`, which is right for a test that segfaults by
    accident and pure noise for one that segfaults on purpose -- it buries the
    assertions under a C traceback per run. Only ever called in the child, and
    only immediately before it is killed.
    """
    import faulthandler

    faulthandler.disable()


_CHILD_SCRIPT = (
    "from tests.test_flight_ring_file import _crash_child; "
    "import sys; _crash_child(sys.argv[1], sys.argv[2])"
)


def _crash_child(action: str, path: str) -> None:
    """Publish the requested crash shape in a fresh, single-threaded process."""
    _silence_crash_reporting()
    recorder = _recorder(path)
    if action == "in-flight":
        _serve(recorder, 2)
        doomed = recorder.begin(1, 1, 0)
        doomed.route(7, 3)
        recorder.publish_log(
            LogCell(
                request_id=doomed.request_id,
                site_id=5,
                severity=Severity.INFO,
                args=(LogArg.text("charging card"),),
            ).encode()
        )
        os.kill(os.getpid(), signal.SIGSEGV)
        raise RuntimeError("SIGSEGV returned")
    if action not in {"publish-segv", "publish-kill"}:
        raise ValueError(f"unknown crash-child action {action!r}")
    _serve(recorder, 5)
    recorder.publish_log(
        LogCell(
            request_id=99,
            site_id=3,
            severity=Severity.ERROR,
            args=(LogArg.integer(4242),),
        ).encode()
    )
    fatal = signal.SIGSEGV if action == "publish-segv" else signal.SIGKILL
    os.kill(os.getpid(), fatal)
    raise RuntimeError(f"signal {fatal} returned")


def _run_crash_child(path: str, action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, action, path],
        capture_output=True,
        text=True,
        check=False,
    )


#: AddressSanitizer installs its own `SIGSEGV` handler, so a child told to
#: segfault reports an ASan error and exits normally instead of dying by signal.
#: The claim under test still holds there, but the *mechanism* assertion cannot,
#: so the SIGSEGV variant is skipped under a sanitized build. Nothing is lost:
#: the SIGKILL variant proves the same thing and no handler can intercept it.
_SANITIZED = "ASAN_OPTIONS" in os.environ


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")
@pytest.mark.skipif(_SANITIZED, reason="ASan intercepts SIGSEGV; the SIGKILL case covers it")
def test_records_survive_a_child_that_segfaults(ring_path) -> None:
    """The load-bearing case: the process dies mid-work, the records remain.

    A real `SIGSEGV`, raised from inside the child, is the failure this feature
    exists for -- nothing is flushed, no handler runs, the process simply stops
    existing. The pages are the kernel's, so the file is intact anyway.
    """
    child = _run_crash_child(ring_path, "publish-segv")
    assert child.returncode == -signal.SIGSEGV, child.stderr

    ring = read_ring_file(ring_path)
    assert ring.live == 6, "five completions and a log record should have survived"
    assert ring.undecodable == 0
    assert not ring.cursors_inconsistent
    assert ring.header.pid != os.getpid(), "the header should name the dead process"

    completions = ring.of_kind(EventKind.COMPLETION)
    assert len(completions) == 5
    assert [record.decode().request_id for record in completions] == [1, 2, 3, 4, 5]

    logs = ring.of_kind(EventKind.LOG)
    assert len(logs) == 1
    assert logs[0].decode().args[0].number == 4242


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")
def test_records_survive_a_child_that_is_killed(ring_path) -> None:
    """SIGKILL: no signal handler could have run even in principle."""
    child = _run_crash_child(ring_path, "publish-kill")
    assert child.returncode == -signal.SIGKILL, child.stderr
    assert read_ring_file(ring_path).live == 6


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")
@pytest.mark.skipif(_SANITIZED, reason="ASan intercepts SIGSEGV")
def test_the_request_in_flight_when_it_died_is_the_one_with_no_completion(
    ring_path,
) -> None:
    """What was it doing when it died?

    A completion cell is written when a request *finishes*, so the request that
    killed the process is precisely the one that has no completion. Its log
    records are on the ring and carry its id, which is what makes the gap
    legible rather than merely present.
    """
    child = _run_crash_child(ring_path, "in-flight")
    assert child.returncode == -signal.SIGSEGV, child.stderr

    ring = read_ring_file(ring_path)
    completed = {
        record.decode().request_id for record in ring.of_kind(EventKind.COMPLETION)
    }
    logged = {record.decode().request_id for record in ring.of_kind(EventKind.LOG)}
    in_flight = logged - completed
    assert completed == {1, 2}
    assert in_flight == {3}, "the doomed request should be the one without a completion"

    record = next(
        r for r in ring.of_kind(EventKind.LOG) if r.decode().request_id == 3
    )
    assert record.decode().args[0].text_value == "charging card"


# --- the file, read back ----------------------------------------------------


def test_the_file_is_exactly_its_own_geometry(ring_path) -> None:
    recorder = _recorder(ring_path, records=16)
    _serve(recorder, 3)
    assert os.path.getsize(ring_path) == ring_file_bytes(16)
    header = RingFileHeader.decode(open(ring_path, "rb").read())
    assert header.ring_records == 16
    assert header.cell_size == CELL_SIZE
    assert header.pid == os.getpid()
    assert header.epoch_unix_ns > 0, "a decoder needs the wall-clock pair"
    del recorder


def test_a_drained_ring_reports_what_the_projector_already_took(ring_path) -> None:
    """Drained records are not lost, and the file must not imply that they are."""
    recorder = _recorder(ring_path)
    _serve(recorder, 4)
    recorder.drain(2)
    ring = read_ring_file(ring_path)
    assert ring.drained == 2, "the tail mirror should say how far the reader got"
    assert ring.live == 2
    assert [r.sequence for r in ring.records] == [2, 3]


def test_a_full_ring_refuses_and_the_file_says_so(ring_path) -> None:
    """The counter that decides whether the file is the story or a sample.

    The ring drops rather than overwrites, so once it is full the records
    nearest a crash are the ones *missing*. A post-mortem that could not see
    that count would read the file as complete.
    """
    recorder = _recorder(ring_path, records=4)
    _serve(recorder, 10)
    ring = read_ring_file(ring_path)
    assert ring.live == 4
    assert ring.ring_full_drops == 6
    assert ring.header.loss(LossReason.RING_FULL) == 6


def test_an_unmapped_recorder_still_works_and_writes_no_file(tmp_path) -> None:
    """The mirrors are unconditional stores; with no file they go to scratch."""
    recorder = _flight.Recorder(
        _flight.MODE_PULSE, ring_records=8, active_requests=8, ring_path=None
    )
    _serve(recorder, 3)
    assert len(recorder.drain(8)) == 3 * CELL_SIZE
    assert list(tmp_path.iterdir()) == []


def test_a_stale_file_is_resized_rather_than_read_as_this_run(ring_path) -> None:
    """A previous, larger run must not leave cells this run would claim."""
    big = _recorder(ring_path, records=64)
    _serve(big, 40)
    del big
    small = _recorder(ring_path, records=8)
    _serve(small, 2)
    assert os.path.getsize(ring_path) == ring_file_bytes(8)
    ring = read_ring_file(ring_path)
    assert ring.header.ring_records == 8
    assert ring.live == 2


# --- the reader, when the file is damaged -----------------------------------


def _write_cursor(path: str, head: int, tail: int) -> None:
    with open(path, "r+b") as handle:
        handle.seek(RING_FILE_CURSOR_OFFSET)
        handle.write(struct.pack("<QQ", head, tail))


def test_a_torn_cursor_pair_is_clamped_and_reported(ring_path) -> None:
    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 3)
    del recorder
    _write_cursor(ring_path, head=3, tail=99)  # tail past head: impossible
    ring = read_ring_file(ring_path)
    assert ring.cursors_inconsistent
    assert ring.live == 0, "a clamped window is empty, never negative"


def test_a_window_wider_than_the_ring_is_clamped_and_reported(ring_path) -> None:
    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 8)
    del recorder
    _write_cursor(ring_path, head=1000, tail=0)  # the ring refuses; it cannot lap
    ring = read_ring_file(ring_path)
    assert ring.cursors_inconsistent
    assert ring.live <= 8


def test_a_cell_that_will_not_decode_costs_only_itself(ring_path) -> None:
    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 4)
    del recorder
    with open(ring_path, "r+b") as handle:
        # Byte 1 is the kind; byte 0 is the schema version. A plausible kind
        # with a wrecked version is what a half-written cell looks like.
        handle.seek(RING_FILE_HEADER_BYTES + CELL_SIZE)
        handle.write(bytes([0xFF, int(EventKind.COMPLETION)]))
    ring = read_ring_file(ring_path)
    assert ring.undecodable == 1
    assert ring.live == 3, "the other three must still be readable"


def test_a_file_that_is_not_a_ring_file_is_refused(tmp_path) -> None:
    path = tmp_path / "nope.bin"
    path.write_bytes(b"\x00" * (RING_FILE_HEADER_BYTES + CELL_SIZE))
    with pytest.raises(Exception, match="not a wreath ring file"):
        read_ring_file(path)


def test_a_truncated_file_is_refused_rather_than_half_read(ring_path) -> None:
    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 4)
    del recorder
    blob = open(ring_path, "rb").read()
    with open(ring_path, "wb") as handle:
        handle.write(blob[: RING_FILE_HEADER_BYTES + 2 * CELL_SIZE])
    with pytest.raises(Exception, match="declares 8 records"):
        read_ring_file(ring_path)


def test_a_ring_path_that_cannot_be_opened_fails_loudly(tmp_path) -> None:
    """A forensic ring nobody notices is missing is worth nothing at the crash."""
    with pytest.raises(OSError):
        _recorder(str(tmp_path / "no-such-directory" / "flight.wfrr"))


def test_offsets_map_onto_a_wall_clock(ring_path) -> None:
    """A cell carries a monotonic offset; the header is what dates it."""
    recorder = _recorder(ring_path)
    _serve(recorder, 1)
    ring = read_ring_file(ring_path)
    assert ring.unix_nano(0) == ring.header.epoch_unix_ns
    assert ring.unix_nano(1_000) == ring.header.epoch_unix_ns + 1_000_000_000


# --- `wreath flight read` ---------------------------------------------------
#
# The operator-facing end. It is reached for exactly once per bad day, by
# someone who is already having one, so the failure modes matter as much as the
# happy path: an unreadable file has to be a message, not a traceback.


def _cli(*argv: str) -> int:
    from wreath import cli

    return cli.main(["flight", *argv])


def test_the_cli_reports_the_records_and_the_provenance(ring_path, capsys) -> None:
    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 3)
    del recorder
    assert _cli("read", ring_path) == 0
    out = capsys.readouterr().out
    assert f"written by pid {os.getpid()}" in out
    assert "3 recovered" in out
    assert out.count("COMPLETION") == 3


def test_the_cli_says_what_the_worker_dropped_before_it_says_what_it_kept(
    ring_path, capsys
) -> None:
    """Ordering is the point, not decoration.

    A reader who sees four records and no drop count concludes those four are
    what happened. The ring refuses when full, so the drop count is what says
    whether the file is the story or the tail of it -- and it has to arrive
    before the records, not after them.
    """
    recorder = _recorder(ring_path, records=4)
    _serve(recorder, 10)
    del recorder
    assert _cli("read", ring_path) == 0
    out = capsys.readouterr().out
    assert "ring_full" in out
    assert out.index("ring_full") < out.index("COMPLETION")


def test_the_cli_filters_by_kind(ring_path, capsys) -> None:
    recorder = _recorder(ring_path, records=16)
    _serve(recorder, 2)
    recorder.publish_log(
        LogCell(request_id=1, site_id=1, severity=Severity.INFO, args=()).encode()
    )
    del recorder
    assert _cli("read", ring_path, "--kind", "log") == 0
    out = capsys.readouterr().out
    assert "LOG" in out
    assert "COMPLETION" not in out


def test_the_cli_emits_versioned_json(ring_path, capsys) -> None:
    import json

    recorder = _recorder(ring_path, records=8)
    _serve(recorder, 2)
    del recorder
    assert _cli("read", ring_path, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["live"] == 2
    assert payload["pid"] == os.getpid()
    assert len(payload["records"]) == 2


def test_the_cli_limits_output_and_says_it_did(ring_path, capsys) -> None:
    recorder = _recorder(ring_path, records=32)
    _serve(recorder, 20)
    del recorder
    assert _cli("read", ring_path, "--limit", "5") == 0
    out = capsys.readouterr().out
    assert out.count("COMPLETION") == 5
    assert "15 more" in out


def test_the_cli_refuses_an_unreadable_file_with_a_message(tmp_path, capsys) -> None:
    """Not a traceback. The person running this has had one crash already."""
    path = tmp_path / "not-a-ring.bin"
    path.write_bytes(b"\x00" * (RING_FILE_HEADER_BYTES + CELL_SIZE))
    assert _cli("read", str(path)) == 2
    assert "not a wreath ring file" in capsys.readouterr().err


def test_the_cli_refuses_a_missing_file_with_a_message(tmp_path, capsys) -> None:
    assert _cli("read", str(tmp_path / "absent.wfrr")) == 2
    assert "absent.wfrr" in capsys.readouterr().err


def test_the_header_round_trips_through_its_own_encoder() -> None:
    header = RingFileHeader(
        ring_records=8,
        cell_size=CELL_SIZE,
        worker_id=3,
        epoch_mono_ns=11,
        epoch_unix_ns=22,
        created_unix_nano=33,
        pid=44,
        head=17,
        tail=5,
        losses=tuple(range(len(LossReason))),
    )
    assert RingFileHeader.decode(header.encode()) == header


def test_the_native_header_and_the_python_one_agree(ring_path) -> None:
    """The mirror that matters: C writes it, Python reads it.

    Every other field would fail loudly if the two disagreed. `worker_id` would
    not -- it would just be wrong -- so it is asserted explicitly.
    """
    recorder = _flight.Recorder(
        _flight.MODE_PULSE,
        worker_id=5,
        ring_records=8,
        active_requests=8,
        ring_path=ring_path,
    )
    _serve(recorder, 1)
    header = read_ring_file(ring_path).header
    assert header.worker_id == 5
    assert header.head == 1
    assert sys.byteorder == "little" or header.ring_records == 8
