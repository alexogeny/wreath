"""Pure-Python twins of the wreath._native._core accelerators.

Each module here mirrors the observable behavior of its C counterpart and is
used automatically when the extension is unavailable or WREATH_PURE=1 is set.
The differential tests in tests/ assert native/pure equivalence.
"""
