"""S3-backed artifact storage via `s3path.S3Path` (pathlib over S3).

Idiom: a load/extract decorator pair over `S3Path`, treating S3 keys as filesystem paths.
"""
from __future__ import annotations

import functools
import json

from s3path import S3Path

_ROOT = S3Path("/summit-artifacts")


def artifact_path(kind: str, key: str) -> S3Path:
    return _ROOT / kind / f"{key}.json"


def load_artifact(kind: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(key: str, *args, **kwargs):
            path = artifact_path(kind, key)
            payload = json.loads(path.read_text()) if path.exists() else None
            return fn(key, payload, *args, **kwargs)

        return wrapper

    return decorator


def extract_artifact(kind: str, key: str, payload: dict) -> None:
    artifact_path(kind, key).write_text(json.dumps(payload))
