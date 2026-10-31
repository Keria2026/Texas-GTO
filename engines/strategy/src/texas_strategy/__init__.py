"""Pluribus-inspired strategy foundations, without a trained solver."""

from .provider import PluribusStyleProvider
from .regret import regret_matching

__all__ = ["PluribusStyleProvider", "regret_matching"]
