"""Explicit capability reporting before blueprint and search exist."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Coverage:
    supported: bool
    reasons: tuple[str, ...]


class PluribusStyleProvider:
    name = "pluribus_style"

    def coverage(self, rules_version: str) -> Coverage:
        reasons = ("blueprint_not_available", "subgame_search_not_implemented")
        if not rules_version.strip():
            reasons = ("rules_version_missing", *reasons)
        return Coverage(supported=False, reasons=reasons)
