"""Star and fork counts for the header link, read once at build time.

mkdocs-material asks the *reader's* browser for these numbers. That spends every
visitor's share of GitHub's 60-requests-an-hour anonymous budget, so the counts
are frequently absent for exactly the people who arrived from a search engine,
and it turns a static page into one that phones a third party. Reading them at
build time instead keeps the promise the rest of this package makes -- a built
page reaches nothing -- and costs one request per build.

The trade is that the numbers are as old as the last deploy. For a star count
that is the right trade: nobody refreshes a docs page to watch it tick.

Nothing here may fail a build. No network, a slow host, a rate limit, a moved
repository: the link renders without counts and the report carries a warning.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import Repo

#: How long to wait for the host's API before giving up on the counts.
_TIMEOUT = 4.0

#: The only two hosts we will call, and the only scheme we will call them over.
#: The URL is built here from a parsed `owner/name`, never taken from config, so
#: a typo in `Repo.url` cannot redirect the build at some other service.
_API = {
    "github": "https://api.github.com/repos/{slug}",
    "gitlab": "https://gitlab.com/api/v4/projects/{quoted}",
}

#: One process, one request per repository. `wreath docs serve` rebuilds on every
#: save, and a watcher that re-asked GitHub each time would be rate-limited
#: within a minute of ordinary editing.
_CACHE: dict[str, tuple[int, int]] = {}


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """What the header needs to draw the repository link."""

    url: str
    title: str
    host: str
    #: `-1` means "not known" -- either not asked for, or the fetch failed.
    stars: int = -1
    forks: int = -1


def describe(repo: Repo, warnings: list[str]) -> RepoInfo:
    """Resolve `repo` into what the header draws, fetching counts if asked."""
    info = RepoInfo(repo.url, repo.title(), repo.host())
    if not repo.stats:
        return info
    slug = repo.slug()
    if not slug:
        warnings.append(f"repo stats: cannot read owner/name out of {repo.url}")
        return info
    counts = _counts(repo.host(), slug, warnings)
    if counts is None:
        return info
    stars, forks = counts
    return RepoInfo(repo.url, repo.title(), repo.host(), stars, forks)


def _counts(host: str, slug: str, warnings: list[str]) -> tuple[int, int] | None:
    key = f"{host}:{slug}"
    if key in _CACHE:
        return _CACHE[key]
    if os.environ.get("WREATH_DOCS_OFFLINE"):
        return None  # asked for silence; not a failure
    payload = _get(host, slug, warnings)
    if payload is None:
        return None
    if host == "github":
        counts = (int(payload.get("stargazers_count", 0)), int(payload.get("forks_count", 0)))
    else:
        counts = (int(payload.get("star_count", 0)), int(payload.get("forks_count", 0)))
    _CACHE[key] = counts
    return counts


def _get(host: str, slug: str, warnings: list[str]) -> dict | None:
    url = _API[host].format(slug=slug, quoted=urllib.parse.quote(slug, safe=""))
    # S310 asks whether the scheme is attacker-controlled. It is not: the URL is
    # one of the two literal https templates above with a parsed `owner/name`
    # quoted into it, so there is no `file:` or custom scheme to reach here even
    # if `Repo.url` is nonsense.
    request = urllib.request.Request(url, headers=_headers(host))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            body = response.read(1 << 20)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        warnings.append(
            f"repo stats: {host} did not answer for {slug} ({exc}); "
            "the header link is rendered without counts"
        )
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        warnings.append(f"repo stats: {host} returned something that is not JSON for {slug}")
        return None
    if not isinstance(payload, dict):
        warnings.append(f"repo stats: unexpected {host} response shape for {slug}")
        return None
    return payload


def _headers(host: str) -> dict[str, str]:
    """Identify the build, and use a token when CI hands us one.

    Anonymous GitHub API calls are limited per source address, which on a shared
    CI runner is a limit shared with every other project on that machine. The
    workflow passes its own `GITHUB_TOKEN`; with it the budget is per-repository
    and the counts stop appearing and disappearing between deploys.
    """
    headers = {"User-Agent": "wreath-docs", "Accept": "application/json"}
    if host == "github":
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
    return headers


def compact(count: int) -> str:
    """`1234` -> `"1.2k"`. Four digits of precision on a star count is noise."""
    if count < 0:
        return ""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")


__all__ = ["RepoInfo", "compact", "describe"]
