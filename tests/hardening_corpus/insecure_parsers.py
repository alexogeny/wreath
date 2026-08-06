"""a10, a11 -- two parsers handed request text with their defences down.

The XML half is the stdlib reader with external general entities switched back
on, which turns a document parser into a file reader. The template half compiles
attacker-supplied source at request time, which turns a renderer into an
expression evaluator over whatever the context holds.
"""
from __future__ import annotations

import xml.sax  # hardening-expect: unsafe-xml-parser
from xml.sax.handler import feature_external_ges  # hardening-expect: unsafe-xml-parser

from wreath import Request, Router
from wreath.exceptions import BadRequest
from wreath.templates import Template

edi = Router(prefix="/edi")


@edi.post("/legacy")
async def legacy_edi(request: Request) -> dict:
    body = await request.body()
    parser = xml.sax.make_parser()
    parser.setFeature(feature_external_ges, True)  # hardening-expect: unsafe-xml-parser
    handler = _Collector()
    parser.setContentHandler(handler)
    parser.parse(_buffer(body))
    return {"segments": handler.segments}


@edi.post("/preview")
async def preview(request: Request) -> dict:
    payload = await request.json()
    source = str(payload["template"])
    if len(source) > 4096:
        raise BadRequest("templates are capped at 4096 characters")
    template = Template.from_string(source)  # hardening-expect: template-from-request
    return {"rendered": template.render(user=payload.get("user"))}


class _Collector(xml.sax.ContentHandler):
    def __init__(self) -> None:
        super().__init__()
        self.segments: list[str] = []

    def characters(self, content: str) -> None:
        self.segments.append(content)


def _buffer(data: bytes) -> object:
    import io

    return io.BytesIO(data)
