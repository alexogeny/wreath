"""a04, a15 -- a path assembled from request text, and an archive trusted.

`Path(root) / name` looks like containment and is not: an absolute `name`
discards `root` entirely, and `..` walks out of it. The archive half is the same
mistake one layer down -- a member name is attacker-controlled text, and a
symlink member turns a later read into a read of whatever it points at.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from wreath import Router
from wreath.exceptions import NotFound
from wreath.response import Response

files = Router(prefix="/exports")

EXPORT_ROOT = Path("/srv/northwind/exports")


@files.get("/download")
async def download(name: str = "") -> Response:
    target = EXPORT_ROOT / name  # hardening-expect: path-from-request
    try:
        return Response(target.read_bytes())
    except FileNotFoundError:
        raise NotFound(f"no export {name!r}") from None


@files.get("/legacy")
async def legacy(name: str = "") -> Response:
    joined = os.path.join(str(EXPORT_ROOT), name)  # hardening-expect: path-from-request
    return Response(Path(joined).read_bytes())


@files.post("/imports")
async def import_archive(upload: bytes, import_id: str) -> dict:
    destination = EXPORT_ROOT / "imports" / import_id  # hardening-expect: path-from-request
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(_buffer(upload)) as archive:
        archive.extractall(destination)  # hardening-expect: unsafe-archive-extract
    return {"import_id": import_id}


def _buffer(data: bytes) -> object:
    import io

    return io.BytesIO(data)
