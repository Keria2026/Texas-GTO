"""Factory for implemented finite research games."""

from .kuhn import KuhnPoker
from .leduc import LeducPoker


def get_game(game_id: str, num_players: int = 2) -> KuhnPoker | LeducPoker:
    if game_id == "kuhn":
        return KuhnPoker(num_players=num_players)
    if game_id == "leduc":
        return LeducPoker(num_players=num_players)
    raise ValueError(f"Unsupported research game: {game_id}")
