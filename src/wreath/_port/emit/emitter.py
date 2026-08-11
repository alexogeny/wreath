"""The emitter, assembled from one layer per domain.

`_Emitter` defines no methods of its own. Each base is a module in this package
and owns one domain, so a method has exactly one home and a second definition of
it is a name collision rather than a silent override -- which is the failure this
layout exists to make impossible: two `visit_Assign` methods once sat nine
hundred lines apart in a single class, and the earlier one simply never ran.

The bases that carry a dispatcher (`visit_ClassDef`, `visit_FunctionDef`,
`visit_Call`, `visit_Module`) inherit the layers they dispatch into, which is why
the list below is not flat. Nothing else about the order is load-bearing: no name
is defined twice, so the MRO decides nothing at runtime.
"""

from __future__ import annotations

from .calls import _CallRewrite
from .classes import _ClassRewrite
from .imports import _ImportRewrite
from .routes import _RouteRewrite
from .walk import _ModuleWalk


class _Emitter(_ClassRewrite, _RouteRewrite, _CallRewrite, _ModuleWalk, _ImportRewrite):
    pass
