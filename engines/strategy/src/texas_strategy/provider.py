"""Rule-bound strategy access; absent trained entries never become recommendations."""

from dataclasses import dataclass

from .blueprint import Blueprint


@dataclass(frozen=True)
class Coverage:
    supported: bool
    reasons: tuple[str, ...]


class PluribusStyleProvider:
    name = "pluribus_style"

    def __init__(self, blueprint: Blueprint | None = None):
        self.blueprint = blueprint

    def coverage(self, rules_version: str, *, game=None, state=None) -> Coverage:
        reasons = []
        if not rules_version.strip():
            reasons.append("rules_version_missing")
        if self.blueprint is None:
            reasons.append("blueprint_not_available")
        elif self.blueprint.metadata["rules_version"] != rules_version:
            reasons.append("rules_version_mismatch")
        elif game is not None:
            for field in ("game_id", "num_players"):
                if self.blueprint.metadata[field] != getattr(game, field):
                    reasons.append(f"{field}_mismatch")
            if state is not None and not reasons:
                if game.actor(state) is None or game.actor(state) < 0:
                    reasons.append("not_a_player_decision")
                elif not self.blueprint.policy.contains(game, state):
                    reasons.append("information_set_not_trained")
                else:
                    # Also verifies exact legal action ordering, including off-tree sizes.
                    self.blueprint.policy.probabilities(game, state)
        return Coverage(not reasons, tuple(reasons))
