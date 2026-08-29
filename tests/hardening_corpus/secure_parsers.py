from __future__ import annotations

from wreath import Request, Router
from wreath.templates import Template
from wreath.xml import XMLRefusal, parse

edi = Router(prefix="/edi")

PREVIEW = Template.from_string(
    "Hello {{ user.name }}, shipment {{ shipment.reference }} is {{ shipment.status }}.",
    name="preview",
)


@edi.post("/legacy")
async def legacy_edi(request: Request) -> dict:
    body = await request.body()
    try:
        document = parse(body)
    except XMLRefusal as refusal:
        return {"accepted": False, "reason": str(refusal)}
    return {"segments": [element.text or "" for element in document.root]}


@edi.post("/preview")
async def preview(request: Request) -> dict:
    payload = await request.json()
    return {"rendered": PREVIEW.render(user=payload.get("user"), shipment=payload.get("shipment"))}
