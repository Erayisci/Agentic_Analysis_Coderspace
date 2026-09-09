"""Exceptions shared across the backend.

`ValidationError` lives here rather than in one of the validation modules so
that both the identity checks and the continuity checks can raise it without
either importing the other.
"""


class ValidationError(Exception):
    """A data-integrity check failed; the build must abort rather than warn."""
