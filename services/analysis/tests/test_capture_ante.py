"""Synthetic ante events validate replay and rule binding, not live policy strength."""

from copy import deepcopy

import pytest
from test_capture_recommendation import eight_player_events, observation
from test_capture_replay import add_snapshot, opening_events

from texas_analysis.capture_recommendation import prepare_query
from texas_analysis.capture_replay import ReplayUnavailable, replay_capture


@pytest.mark.parametrize("stacks", [(800,) * 4, (800, 41, 800, 800), (800, 45, 800, 800)])
def test_ante_replay_reconstructs_actual_opening_and_short_blinds(stacks):
    events = opening_events(stacks=stacks, ante=4)
    replay = replay_capture(events)
    assert replay.opening_cash == stacks
    assert replay.game.ante == 4
    assert replay.fees == {"hero": 20, "player-1": 40, "player-2": 20, "player-3": 20}
    assert sum(replay.state.stacks) + replay.state.pot + sum(replay.fees.values()) == sum(stacks)
    assert replay.game.amount_to_call(replay.state) == 8
    assert replay.state.contributions[1] == min(6, stacks[1] - 40)


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda e: e[5]["data"].update(amount_delta=5), "unsupported_ante"),
        (lambda e: e[5]["data"].update(amount_delta=3), "conflicting_ante_amount"),
        (lambda e: e[6]["data"].update(player_id="hero"), "invalid_ante_order"),
        (
            lambda e: e[6]["data"].update(action="PAY_MUSHROOM", amount_delta=20),
            "invalid_fee_order",
        ),
    ],
)
def test_incomplete_or_contradictory_ante_cannot_be_reconstructed(change, reason):
    events = opening_events(ante=4)
    change(events)
    with pytest.raises(ReplayUnavailable, match=reason):
        replay_capture(events)


def test_short_straddle_cannot_hide_behind_ante_all_in():
    with pytest.raises(ReplayUnavailable, match="unsupported_forced_bets"):
        replay_capture(opening_events(stacks=(800, 800, 800, 31), ante=4))


def test_pre_deal_cash_may_only_catch_up_by_confirmed_blind_debits():
    events = opening_events(ante=4)
    settled = [p["stack"] for p in events[-1]["data"]["players"]]
    view = events[-1]["data"]
    view["hero_cards"] = [None, None]
    for player, blind in zip(view["players"], (0, 2, 4, 8), strict=True):
        player["stack"] += blind
    with pytest.raises(ReplayUnavailable, match="missing_opening_snapshot"):
        replay_capture(events)
    add_snapshot(events, settled, actor="hero", call=8)
    assert replay_capture(events).opening_cash == (800,) * 4
    for delta in (-1, 1, 20):
        broken = deepcopy(events)
        broken[-2]["data"]["players"][0]["stack"] += delta
        with pytest.raises(ReplayUnavailable, match="snapshot_cash_conflict"):
            replay_capture(broken)


def test_known_deal_cash_cannot_be_reanchored_to_hide_later_changes():
    events = opening_events(ante=4)
    cash = [p["stack"] for p in events[-1]["data"]["players"]]
    cash[3] -= 8
    add_snapshot(events, cash, actor="hero", call=8)
    with pytest.raises(ReplayUnavailable, match="snapshot_cash_conflict"):
        replay_capture(events)


def test_eight_player_query_uses_ante_rules_and_disjoint_policy_keys():
    events = eight_player_events(ante=4)
    model, replay, query = prepare_query(observation(events), events)
    assert model.base_game.ante == replay.game.ante == 4
    assert replay.state.pot == 46
    assert model.base_game.initial_stacks == (800,) * 8
    assert model.initial_state().underlying.table.pool == 180
    assert query.information_key.startswith("ante4:")
    old_events = eight_player_events()
    old, _, old_query = prepare_query(observation(old_events), old_events)
    assert old.rules_version != model.rules_version
    assert not old_query.information_key.startswith("ante4:")
    assert query.information_key != old_query.information_key
    damaged = deepcopy(events)
    damaged[-1]["data"]["players"][0]["stack"] += 4
    changed, _, _ = prepare_query(observation(damaged), damaged)
    assert changed.base_game.initial_stacks[0] == 804  # Do not silently normalize to 800.
