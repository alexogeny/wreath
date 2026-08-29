---
description: Choose MCP, GraphQL, gRPC, Protobuf or webhooks without duplicating application policy.
keywords: guide MCP GraphQL gRPC Protobuf webhooks protocols
---

# Protocols and MCP

Choose the protocol for the caller, then keep the application operation behind it.
Identity, authorization and schema should not fork because the transport changed.

| Caller | Boundary |
|---|---|
| model or agent client | MCP tool, resource or prompt |
| browser asking flexible read questions | GraphQL schema |
| typed internal RPC client | gRPC service and Protobuf messages |
| another system notifying you | verified webhook inbox |
| your service notifying another | durable webhook outbox |

Wreath's Protobuf codec declares stable wire numbers directly in Python:

```python title="wire.py"
from wreath.protobuf import decode, encode, field, message


@message
class Position:
    collar_id: int = field(1)
    latitude: float = field(2)
    longitude: float = field(3)


def round_trip(position: Position) -> Position:
    return decode(Position, encode(position))
```

```python title="test_wire.py"
from wire import Position, round_trip


def test_the_wire_contract_survives_a_round_trip() -> None:
    position = Position(collar_id=7, latitude=-37.81, longitude=144.96)
    assert round_trip(position) == position
```

Field numbers are the compatibility contract; reorderings do not change the wire.
Unknown fields are preserved for version skew. Declarations compile once and the codec
runs through Wreath's native implementation.

Next, [build a complete MCP server](mcp.md), or inspect every
[protocol and delivery API](../reference/protocols.md).
