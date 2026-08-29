"""A camera-trap network, built on wreath.

Four wildlife reserves put motion-triggered cameras in the bush. Cameras fill
SD cards with timestamped images. Volunteers and researchers identify what is
in them. Ecologists ask what moved, where, and when.

This package is wreath's canonical example: one application that uses the
framework's parts together rather than a gallery of snippets that each use one.
"""

from __future__ import annotations

from .models import MODELS, SCHEMA

__all__ = ["MODELS", "SCHEMA"]
