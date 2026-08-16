"""Shared exception hierarchy for every replay surface."""


class ReplayError(Exception):
    """A recording cannot be decoded or reproduced faithfully."""


class HttpReplayError(ReplayError, ValueError):
    """An outbound HTTP exchange is malformed, incomplete, or diverged."""
