# PostgreSQL guidance

Read this before PostgreSQL, ORM, migration, database fixture, notification, or live-database test work.


- **Some tests need a real PostgreSQL, and skipping them used to be silent.**
  Suites gated on `WREATH_TEST_POSTGRES_DSN` cover what a fake cannot model —
  parameter type inference, query plans, lock and timeout behaviour, DST
  boundaries. They went a long time without running once, and when they finally
  did they found a defect in a *default* code path that worked on its first call
  and raised on every call after. `tests/conftest.py` now prints a banner naming
  the count whenever they skip; it never fails the run, because a warning that
  breaks the build gets suppressed. To run them:

  ```bash
  docker run -d --name wreath-test-pg -e POSTGRES_PASSWORD=wreath \
    -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test -p 55432:5432 \
    pgvector/pgvector:pg17 -c max_connections=200 -c fsync=off -c synchronous_commit=off
  export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
  ```

  **The image is `pgvector/pgvector:pg17`, not `postgres:17-alpine`.** It is
  stock PostgreSQL 17 with `pgvector` available to `CREATE EXTENSION`, and the
  vector suites skip on a server without it — which is the silent-skip failure
  mode this section exists to warn about, one layer down. Everything else
  behaves identically.

  `podman` and `nerdctl` work too. Some database suites are also marked
  `network` and so are excluded by the default marker expression entirely —
  `-m ''` includes them.
- **A database fixture must name its schema per xdist worker, and must assign
  the name rather than default it.** Workers sharing one schema race on
  `CREATE SCHEMA IF NOT EXISTS`, which is not atomic against a concurrent
  creator; PostgreSQL reports the race as a `pg_namespace_nspname_index` or
  `pg_type_typname_nsp_index` unique violation, which reads like anything except
  a test-isolation bug. Two suites shipped with this and one of them was flaky
  for days. Derive the name from `PYTEST_XDIST_WORKER`, and use plain assignment
  — **`os.environ.setdefault` in a `conftest` silently does nothing**, because
  the controller imports the conftest during collection, writes the value, then
  spawns workers with *its own* environment, so every worker inherits the
  controller's name and no-ops. That failure looks like the fix not working
  rather than like a mistake in the fix. `tests/_camera_trap.py` and
  `tests/test_replay_live_faults.py` are the patterns to copy.
- **The native driver subclasses the Python one.** `_native._postgres.Connection`
  inherits from `wreath._pgdriver.Connection`, the C pipeline reads fifteen
  module-level names out of that module at init, and `resolve_offsets` resolves
  its `__slots__` byte offsets -- so grepping for a wire constant and finding it
  only in `_pgdriver.py` does **not** mean the native path lacks the feature. One
  session concluded cancellation was unimplemented natively on exactly that
  evidence; the MRO says otherwise.
- **`execute("LISTEN ...")` registers with PostgreSQL but not with the driver**,
  so notifications are never pumped and a listener receives nothing at all.
  `connection.listen()` is the API, and it is why `Doorbell` holds its own
  connection.
