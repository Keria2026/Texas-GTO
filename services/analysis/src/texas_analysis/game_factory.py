from texas_game_core.games import get_game
from texas_game_core.holdem import HoldemGame
from texas_game_core.mushroom_game import MushroomSessionGame

from .models import GameConfig


def make_game(config: GameConfig):
    if config.game_id == "mushroom":
        if config.abstraction == "mushroom_population":
            from texas_strategy.mushroom_population import MushroomPopulationGame

            return MushroomPopulationGame.default(
                horizon=config.session_hands or 2, ante=config.ante
            )
        game = MushroomSessionGame(
            config.stacks if config.stacks is not None else (config.stack,) * config.num_players,
            button=config.button,
            horizon=config.session_hands or 2,
            ante=config.ante,
        )
        if config.abstraction == "mushroom_context":
            from texas_strategy.mushroom_abstraction import MushroomContextGame

            return MushroomContextGame(game)
        return game
    if config.game_id == "holdem":
        game = HoldemGame(
            stacks=config.stacks
            if config.stacks is not None
            else (config.stack,) * config.num_players,
            small_blind=config.small_blind,
            big_blind=config.big_blind,
            straddle=config.straddle,
            ante=config.ante,
            button=config.button,
        )
        if config.abstraction == "suit_canonical":
            from texas_strategy.abstraction import SuitCanonicalHoldem

            return SuitCanonicalHoldem(game)
        return game
    return get_game(config.game_id, num_players=config.num_players)
