"""Wreath's story-led documentation site."""

from __future__ import annotations

from wreath.docs import THEMES, Link, Nav, Page, Repo, Section, Site

nav = Nav(
    Page("Home", "index.md"),
    Section(
        "Stories",
        Page("Build something real", "stories/index.md"),
        Page("Build the app they said was missing", "stories/serious-api.md"),
        Page("Balance a live energy depot", "stories/energy-depot.md"),
        Page("Turn computers into an agent fleet", "stories/agent-fleet.md"),
        Page("Let customers wire the world together", "stories/automation-backplane.md"),
        Page("Give an agent tools safely", "stories/mcp-control-room.md"),
        Page("Land the enterprise", "stories/enterprise.md"),
        Page("Ask better questions of time", "stories/time-series-lab.md"),
        Page("Survive the noon drop", "stories/noon-drop.md"),
        Page("Assume the network will fail", "stories/field-operations.md"),
    ),
    Section(
        "Start",
        Page("Your first Wreath application", "start/index.md"),
        Page("Choose a path", "start/paths.md"),
        Page("Versions and upgrades", "start/releases.md"),
        Page("Wreath 0.4.0", "release_notes/0.4.0.md"),
        Page("Wreath 0.3.4", "release_notes/0.3.4.md"),
    ),
    Section(
        "Guides",
        Page("The expected framework stuff", "guides/index.md"),
        Page("Build an HTTP API", "guides/http-api.md"),
        Page("Configuration and lifecycle", "guides/configuration.md"),
        Page("Browser apps and assets", "guides/browser-apps.md"),
        Page("Identity and users", "guides/identity.md"),
        Page("PostgreSQL and models", "guides/data.md"),
        Page("Migrations from detect to rollback", "guides/migration-workflow.md"),
        Page("Migration architecture and fleet upgrades", "guides/migrations.md"),
        Page("Objects and uploads", "guides/objects.md"),
        Page("Realtime and durable work", "guides/realtime.md"),
        Page("Chunked data passes", "guides/chunked-passes.md"),
        Page("Exactly-once effects", "cookbook/recipes/exactly-once.md"),
        Page("Policy and hardening", "guides/policy.md"),
        Page("Integration boundaries", "guides/integrations.md"),
        Page("Protocols and MCP", "guides/protocols.md"),
        Page("Build an MCP server", "guides/mcp.md"),
        Page("Operations and deployment", "guides/operations.md"),
        Page("Deploy Wreath", "guides/deployment.md"),
        Page("Command-line tasks", "guides/cli.md"),
        Page("Testing and evidence", "guides/testing.md"),
    ),
    Section(
        "Reference",
        Page("The Wreath surface", "reference/index.md"),
        Page("Application and HTTP", "reference/application.md"),
        Page("Policy", "reference/policy.md"),
        Page("Identity and tenancy", "reference/identity.md"),
        Page("Data and analysis", "reference/data.md"),
        Page("Realtime and durable work", "reference/realtime.md"),
        Page("Protocols and delivery", "reference/protocols.md"),
        Page("MCP", "reference/mcp.md"),
        Page("Operations", "reference/operations.md"),
        Page("Tooling", "reference/tooling.md"),
    ),
)

site = Site(
    name="Wreath",
    source="docs",
    output="site",
    nav=nav,
    palette=THEMES["signal"],
    feel="luminous",
    base_url="https://alexogeny.github.io/wreath",
    description=(
        "A Python 3.14-first ASGI framework for realtime systems, durable work, "
        "governed AI and serious multi-tenant applications."
    ),
    source_url="https://github.com/alexogeny/wreath/edit/main/docs",
    repo=Repo("https://github.com/alexogeny/wreath"),
    links=(
        Link("Package", "https://pypi.org/project/wreath/", "package"),
        Link("Issues", "https://github.com/alexogeny/wreath/issues", "github"),
    ),
    map_page="reference/index.md",
)
