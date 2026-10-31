from texas_game_core.games import get_game
from texas_game_core.holdem import HoldemGame

from .models import GameConfig


def make_game(config: GameConfig):
    if config.game_id == "holdem":
        game = HoldemGame(
            stacks=(config.stack,) * config.num_players,
            small_blind=config.small_blind,
            big_blind=config.big_blind,
            button=config.button,
        )
        if config.abstraction == "suit_canonical":
            from texas_strategy.abstraction import SuitCanonicalHoldem

            return SuitCanonicalHoldem(game)
        return game
    return get_game(config.game_id, num_players=config.num_players)
