"""从模拟会话公开事件重建画像；按事件版本截断，不读取底牌内容。"""

from collections.abc import Sequence

from texas_game_core.interfaces import CHANCE_PLAYER
from texas_strategy.opponent import MODEL_VERSION, OpponentModel, ProfileEvent, ScopedPlayer


def _family(action: str) -> str:
    return "raise_to" if action.startswith("raise_to:") else action


def _context(game, state, player: int, support: tuple[str, ...]) -> str:
    facing = "facing_wager" if "call" in support else "no_wager"
    if game.game_id == "holdem":
        relative = (player - game.button) % game.num_players
        return f"holdem:{state.street_name}:position_{relative}:{facing}"
    if game.game_id == "leduc":
        street = "before_board" if state.board is None else "after_board"
        return f"leduc:{street}:seat_{player}:{facing}"
    return f"kuhn:single_round:seat_{player}:{facing}"


def summarize_profiles(
    game,
    actions: Sequence[str],
    *,
    session_id: str,
    as_of: int | None = None,
) -> dict[str, object]:
    """Return session-scoped Bayesian action statistics for one simulated hand.

    ``actions`` includes private/public chance events. ``as_of`` is the number
    of applied events, matching the storage state_version convention. Future
    events are neither replayed nor inspected. VPIP/PFR become available at
    hand completion and count only seats actually dealt two private cards.

    Exact integer raises are grouped into the explicitly named ``raise_to``
    family. This is an action-family distribution, not a model over every chip
    size and not an inference about private cards or profitable adaptations.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a nonempty string")
    if isinstance(actions, str) or not isinstance(actions, Sequence):
        raise ValueError("actions must be an ordered sequence of events")
    cutoff = len(actions) if as_of is None else as_of
    if type(cutoff) is not int or not 0 <= cutoff <= len(actions):
        raise ValueError("as_of must be an event version between zero and len(actions)")
    if game.game_id not in {"holdem", "kuhn", "leduc"}:
        raise ValueError("profiles support only the implemented simulation games")

    is_holdem = game.game_id == "holdem"
    players = tuple(
        ScopedPlayer(session_id, "simulation", str(player)) for player in range(game.num_players)
    )
    model = OpponentModel()
    contexts: list[set[tuple[str, tuple[str, ...]]]] = [set() for _ in players]
    private_deals = [0] * game.num_players
    voluntary = [False] * game.num_players
    raised_preflop = [False] * game.num_players
    terminal_at: int | None = None
    state = game.initial_state()

    for version, action in enumerate(actions[:cutoff], start=1):
        actor = game.actor(state)
        if actor is None:
            raise ValueError("an event follows the completed hand")
        if not isinstance(action, str):
            raise ValueError("each event action must be a string")
        next_state = game.transition(state, action)
        if actor == CHANCE_PLAYER:
            if is_holdem and state.pending_deal[0] >= 0:
                # Recipient is public; the value of the private card is never
                # read, summarized, or used as an action conditioning feature.
                private_deals[state.pending_deal[0]] += 1
        else:
            support = tuple(sorted({_family(legal) for legal in game.legal_actions(state)}))
            family = _family(action)
            context = _context(game, state, actor, support)
            model.record(
                ProfileEvent(
                    event_id=f"event:{version}:action",
                    player=players[actor],
                    context=context,
                    observed_at=version,
                    kind="action",
                    action=family,
                    legal_actions=support,
                )
            )
            contexts[actor].add((context, support))
            if is_holdem and state.street == 0:
                if family == "call" and game.amount_to_call(state, actor) > 0:
                    voluntary[actor] = True
                elif family == "raise_to":
                    voluntary[actor] = True
                    raised_preflop[actor] = True
        state = next_state
        if game.actor(state) is None:
            terminal_at = version
            if is_holdem:
                for player, identity in enumerate(players):
                    if private_deals[player] != 2:
                        continue
                    for name, successful in (
                        ("VPIP", voluntary[player]),
                        ("PFR", raised_preflop[player]),
                    ):
                        model.record(
                            ProfileEvent(
                                event_id=f"hand:0:{name}",
                                player=identity,
                                context="holdem:completed_hand",
                                observed_at=version,
                                available_at=version,
                                kind="opportunity",
                                name=name,
                                success=successful,
                            )
                        )

    result_players = []
    for player, identity in enumerate(players):
        statistics = {}
        if is_holdem:
            for name in ("VPIP", "PFR"):
                posterior = model.stat(identity, "holdem:completed_hand", name, as_of=cutoff)
                statistics[name] = {
                    "successes": posterior.successes,
                    "opportunities": posterior.opportunities,
                    "posterior_mean": posterior.mean,
                    "interval": list(posterior.interval()),
                    "interval_method": posterior.interval_method,
                    "model_version": posterior.model_version,
                }
        distributions = []
        for context, support in sorted(contexts[player]):
            posterior = model.actions(identity, context, support, as_of=cutoff)
            distributions.append(
                {
                    "context": context,
                    "legal_actions": list(support),
                    "counts": dict(posterior.counts),
                    "probabilities": dict(posterior.probabilities),
                    "observations": posterior.observations,
                    "action_encoding": "family",
                    "model_version": posterior.model_version,
                }
            )
        result_players.append(
            {
                "player_id": identity.player_id,
                "seat": player,
                "statistics": statistics,
                "actions": distributions,
            }
        )

    warnings = [
        "仅统计当前模拟会话；座位身份不跨会话合并。",
        "单手样本向先验收缩，不能据此确定牌风、底牌范围或剥削收益。",
        "行动频率按公开情境与合法动作族分组；raise_to 金额合并，不预测具体下注尺寸。",
        "统计区间采用保守的后验质量界，不是等尾分位可信区间。",
    ]
    if is_holdem and terminal_at is None:
        warnings.append("本手尚未完成；VPIP/PFR 暂不增加机会分母。")
    if not is_holdem:
        warnings.append("Kuhn/Leduc 仅提供行动统计，不生成德州 VPIP/PFR。")
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "state_version": cutoff,
        "model_version": MODEL_VERSION,
        "source": "simulation",
        "hand_complete": terminal_at is not None,
        "players": result_players,
        "warnings": warnings,
    }
