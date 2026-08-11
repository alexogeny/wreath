"""Elmshaw route table — literal `add_route` calls, one per pattern.

`config.add_route(name, pattern)` with two string literals is the whole of URL
dispatch, and the pattern is already wreath's: `{hedge_id}` is `{hedge_id}`.
The name is the join to the `view_config` next door, and it is also what
`request.route_url` reads — `Wreath.url_path_for(name, **parameters)` is the
same lookup by the same key, so `name=` on the route carries it across.

`config.scan()` and `config.include()` are deliberately absent. Those decide at
startup what this application serves, and the list they produce is not in any
source file — see `foreign/mosswood_almanac/`.
"""

from pyramid.config import Configurator


def make_app(settings):
    config = Configurator(settings=settings)
    config.add_route("healthz", "/healthz")
    config.add_route("hedges", "/hedges")
    config.add_route("hedge", "/hedges/{hedge_id}")
    config.add_route("hedge_survey", "/hedges/{hedge_id}/surveys/{year}")
    config.add_route("hedge_measure", "/hedges/{hedge_id}/measure")
    config.add_route("hedge_trace", "/hedges/{hedge_id}/trace")
    config.add_route("hedge_retire", "/hedges/{hedge_id}")
    config.add_route("hedge_legacy", "/legacy/hedges/{hedge_id}")
    return config.make_wsgi_app()
