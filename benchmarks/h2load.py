"""Drive `h2load` as a subprocess, for the protocols the built-in generator cannot.

`benchmarks/load.py` speaks HTTP/1.1 over cleartext and nothing else. Measuring
HTTP/2 and HTTP/3 needs a real client for those protocols, and importing one
into the runner would make the runner's own event loop part of the measurement.
So this shells out to `h2load` (from nghttp2) and parses what it reports.

Two things here are load-bearing:

**The negotiated protocol is verified, never assumed.** `h2load` accepts `--h3`
on a build compiled without HTTP/3, ignores it, and exits 0 -- so asking for h3
and getting a clean run proves nothing. It does print the ALPN it actually
negotiated, and `measure()` refuses any run whose reported protocol is not the
one that was asked for. A row labelled `h3` therefore means h3 happened.

**Percentiles come from the per-request log, not the summary.** `h2load`'s
summary reports min/max/mean/sd, which cannot produce the p95 and p99 the report
compares. `--log-file` writes one line per request with the response time in
microseconds; those are what the percentiles are computed from.
"""

from __future__ import annotations

import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .load import Result

LOAD_GENERATOR = "h2load"

#: What to pass h2load for each protocol we measure.
_ALPN_FLAGS: dict[str, list[str]] = {
    "http/1.1": ["--alpn-list=http/1.1"],
    "h2": ["--alpn-list=h2"],
    "h3": ["--h3"],
}
#: What h2load calls the protocol back to us, per requested protocol.
_EXPECTED_ALPN: dict[str, str] = {"http/1.1": "http/1.1", "h2": "h2", "h3": "h3"}

_FINISHED = re.compile(r"finished in\s+([0-9.]+)(us|ms|s),\s+([0-9.]+)\s+req/s")
_REQUESTS = re.compile(
    r"requests:\s+(\d+)\s+total,\s+\d+\s+started,\s+\d+\s+done,\s+(\d+)\s+succeeded,"
    r"\s+(\d+)\s+failed,\s+(\d+)\s+errored,\s+(\d+)\s+timeout"
)
_APPLICATION_PROTOCOL = re.compile(r"Application protocol:\s*(\S+)")
_DURATION_SCALE = {"us": 1e-6, "ms": 1e-3, "s": 1.0}


class H2LoadError(RuntimeError):
    """h2load could not run, or did not do what it was asked."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    path: str
    http3: bool


def capabilities() -> Capabilities | None:
    """Locate h2load and determine whether this build speaks HTTP/3.

    `h2load --version` prints the same string whether or not HTTP/3 was compiled
    in, and `--help` lists the `--h3` flag either way, so neither can answer
    this. The linked QUIC libraries can: a build without HTTP/3 has no ngtcp2.
    """
    path = shutil.which("h2load")
    if path is None:
        return None
    try:
        linkage = subprocess.run(["ldd", path], capture_output=True, text=True, timeout=20).stdout
    except OSError, subprocess.SubprocessError:
        # Not Linux, or no ldd. Assume HTTP/3 is present and let the negotiated
        # protocol check below catch it if it is not; a false "no h3" here would
        # skip a protocol that works.
        return Capabilities(path, True)
    return Capabilities(path, "ngtcp2" in linkage)


def _percentiles(log: Path) -> tuple[float, float, float]:
    """Median, p95, p99 in milliseconds, from h2load's per-request log.

    Columns are tab-separated: start time (us since epoch), status code, and
    microseconds to the end of the response. Only successful requests count --
    a connection refused in microseconds would otherwise look like the fastest
    response in the run.
    """
    samples: list[float] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        try:
            status, micros = int(fields[1]), float(fields[2])
        except ValueError:
            continue
        if 200 <= status < 400:
            samples.append(micros / 1000.0)
    if not samples:
        return 0.0, 0.0, 0.0
    samples.sort()

    def at(fraction: float) -> float:
        index = min(len(samples) - 1, int(len(samples) * fraction))
        return samples[index]

    return statistics.median(samples), at(0.95), at(0.99)


def measure(
    host: str,
    port: int,
    path: str,
    protocol: str,
    *,
    requests: int,
    connections: int,
    streams_per_connection: int = 1,
    threads: int = 1,
    warmup_requests: int = 0,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    timeout: float = 300.0,
    tls: bool = True,
) -> Result:
    """Run `requests` requests over `protocol` and report what happened.

    Raises `H2LoadError` rather than returning a plausible-looking Result when
    h2load is missing, cannot serve the protocol, or negotiated something other
    than what was asked for.
    """
    if protocol not in _ALPN_FLAGS:
        raise H2LoadError(f"h2load cannot measure {protocol!r}")
    found = capabilities()
    if found is None:
        raise H2LoadError(
            "h2load is not on PATH; install nghttp2's tools (see benchmarks/README.md)"
        )
    if protocol == "h3" and not found.http3:
        raise H2LoadError(
            "this h2load was built without HTTP/3, and would accept --h3, ignore it, "
            "and exit 0. Rebuild nghttp2 with --enable-http3 (benchmarks/README.md)."
        )
    if body and method != "POST":
        raise H2LoadError("the h2load path only sends a request body with POST")
    if not method.isalpha() or not method.isupper():
        raise H2LoadError(f"suspicious HTTP method for h2load: {method!r}")

    # h2 and h3 require TLS. HTTP/1.1 may be measured cleartext (tls=False) so a
    # plaintext-h1 run is not bottlenecked on the built-in Python client;
    # cleartext and TLS rows are never mixed in one run (the caller passes the
    # run's actual TLS state), so the "encryption tax" comparison stays honest.
    cleartext = not tls and protocol == "http/1.1"
    scheme = "http" if cleartext else "https"
    url = f"{scheme}://{host}:{port}{path}"
    with tempfile.TemporaryDirectory() as directory:
        log = Path(directory) / "requests.tsv"
        data = Path(directory) / "request-body.bin"
        # h2load cannot mmap an empty --data file; an empty-body POST goes
        # through the :method override below instead, like PUT/PATCH/DELETE.
        send_data = method == "POST" and bool(body)
        if send_data:
            data.write_bytes(body)

        def command_for(count: int, *, measured: bool) -> list[str]:
            command = [
                found.path,
                "-n",
                str(count),
                "-c",
                str(connections),
                "-t",
                str(min(threads, connections)),
                "-m",
                str(streams_per_connection),
                *([f"--log-file={log}"] if measured else []),
                *(["--data", str(data)] if send_data else []),
                # h2load's request method is GET (or POST with --data); the
                # :method pseudo-header override rewrites it for every
                # protocol, including the HTTP/1.1 request line (verified:
                # a method-discriminating server sees the real method).
                *(["-H", f":method: {method}"] if method != "GET" and not send_data else []),
                *(["--h1"] if cleartext else _ALPN_FLAGS[protocol]),
            ]
            for name, value in (headers or {}).items():
                command += ["-H", f"{name}: {value}"]
            command.append(url)
            return command

        def invoke(count: int, *, measured: bool) -> str:
            try:
                completed = subprocess.run(
                    command_for(count, measured=measured),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                raise H2LoadError(f"h2load timed out after {timeout}s") from error
            output = completed.stdout + completed.stderr
            if completed.returncode != 0:
                phase = "measurement" if measured else "warmup"
                raise H2LoadError(
                    f"h2load {phase} exited {completed.returncode}: {output.strip()[:400]}"
                )
            return output

        if warmup_requests > 0:
            invoke(warmup_requests, measured=False)
        output = invoke(requests, measured=True)

        if not cleartext:  # cleartext h1 has no ALPN to verify
            negotiated = _APPLICATION_PROTOCOL.search(output)
            expected = _EXPECTED_ALPN[protocol]
            if negotiated is None:
                raise H2LoadError(
                    f"h2load did not report a negotiated protocol; asked for {protocol}. "
                    f"Output: {output.strip()[:300]}"
                )
            if negotiated.group(1) != expected:
                raise H2LoadError(
                    f"asked for {protocol}, but the connection negotiated "
                    f"{negotiated.group(1)!r}. Refusing to record this as {protocol}."
                )

        finished = _FINISHED.search(output)
        counted = _REQUESTS.search(output)
        if finished is None or counted is None:
            raise H2LoadError(f"could not parse h2load output: {output.strip()[:300]}")
        duration = float(finished.group(1)) * _DURATION_SCALE[finished.group(2)]
        succeeded = int(counted.group(2))
        errors = int(counted.group(3)) + int(counted.group(4)) + int(counted.group(5))
        median, p95, p99 = _percentiles(log)

    return Result(
        requests=succeeded,
        errors=errors,
        duration_seconds=duration,
        requests_per_second=float(finished.group(3)),
        latency_ms_median=median,
        latency_ms_p95=p95,
        latency_ms_p99=p99,
    )
