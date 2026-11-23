"""A generated source file and the reason for an optional fallback."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Translation:
    code: str | bytes
    fallback_reason: str | None = None
    requires_python: bool = False
