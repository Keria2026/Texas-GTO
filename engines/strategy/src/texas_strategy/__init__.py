"""Pluribus-inspired offline training, coverage checks and bounded research search."""

from .provider import PluribusStyleProvider
from .regret import regret_matching

__all__ = ["PluribusStyleProvider", "regret_matching"]
