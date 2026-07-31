"""What the generic request pipeline costs a request that uses none of it.

`Wreath._handle_http` decides, on every request, questions that were settled at
`_compile_routes()` time: are there global hooks, are there stage hooks, is
there a dynamic matcher, is the routing table classifying, does this route carry
an authorization requirement, is a recorder armed. An application with none of
those still walks every one of those branches to find out it has none of them.

This benchmark measures that walk by running both dispatchers as interleaved
arms **inside one process**, against the same compiled application. That matters
more here than anywhere else in the repository: relinking `_core` perturbs
unrelated functions by 10-18%, so a rebuild between arms cannot resolve a
single-digit change at all. Two dispatchers in one binary can.

The A/A control is the same generic dispatcher entered as a second arm, placed
at the far end of the round from its twin, so the floor includes within-round
drift rather than flattering itself with adjacency.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Any

from wreath import Wreath
from wreath._devtools.measure import Arm, report, run_apps, scope


def build_application() -> Wreath:
    """The shape the specialization is for: no middleware, no auth, one route."""
    app = Wreath()

    @app.get("/items/{item_id}")
    async def item(request: Any) -> dict[str, Any]:
        return {"id": request.path_params["item_id"], "ok": True}

    app._compile_routes()
    return app


def generic_application() -> Wreath:
    """The same application with specialization forced off."""
    app = build_application()
    app._dispatch_http = app._handle_http
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=4000)
    parser.add_argument("--warmup", type=int, default=4000)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    arms = [
        Arm("generic", app=generic_application()),
        Arm("specialised", app=build_application()),
        # Far end of the round from `generic`, deliberately.
        Arm("generic-control", app=generic_application()),
    ]
    template = scope("GET", "/items/42")

    run_apps(arms, template, args.rounds, args.iterations, args.warmup)
    print(f"python {platform.python_version()} on {platform.platform()}\n")
    payload = report(arms, "generic", "generic-control")

    if args.json is not None:
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
