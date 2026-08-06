"""a04 and a15 written correctly. Nothing here may produce a finding.

Containment is not a check the handler performs; it is a property of the API it
reaches for. `normalize_key` refuses a traversal once, for every backend, and
`LocalStorage` opens beneath a root descriptor so the file that was checked is
the file that is read.
"""
from __future__ import annotations

from pathlib import Path

from wreath import Router
from wreath.exceptions import NotFound
from wreath.response import Response
from wreath.storage import LocalStorage, StorageError, normalize_key, unzip_stream

files = Router(prefix="/exports")

EXPORT_ROOT = Path("/srv/northwind/exports")
exports = LocalStorage(EXPORT_ROOT)


@files.get("/download")
async def download(name: str = "") -> Response:
    try:
        key = normalize_key(name)
    except StorageError:
        raise NotFound(f"no export {name!r}") from None
    return Response(await exports.get(key))


@files.get("/manifest")
async def manifest() -> Response:
    # A constant path is nobody's traversal, and the rule has to leave it alone.
    return Response((EXPORT_ROOT / "manifest.json").read_bytes())


@files.post("/imports")
async def import_archive(upload: bytes, import_id: str) -> dict:
    destination = normalize_key(f"imports/{import_id}")
    written = 0
    async for member, chunk in unzip_stream(upload):
        await exports.put(normalize_key(f"{destination}/{member}"), chunk)
        written += 1
    return {"import_id": import_id, "members": written}
