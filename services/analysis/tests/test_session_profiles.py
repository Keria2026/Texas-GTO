"""Replay-derived session statistics must respect opportunities and time."""

import json

import pytest

from texas_analysis.session_profiles import summarize_profiles
from texas_game_core.holdem import HoldemGame
from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker

PRIVATE_DEAL = ("deal:Ks", "deal:As", "deal:Kh", "deal:Ah")
RUNOUT = ("deal:2c", "deal:3d", "deal:4h", "deal:7s", "deal:9c")
ALL_IN = (*PRIVATE_DEAL, "raise_to:20", "call", *RUNOUT)


def _player(result, seat):
    player = next(item for item in result["players"] if item["seat"] == seat)
    assert player["player_id"] == str(seat)
    return player


def _assert_stat(player, name, successes, opportunities):
    stat = player["statistics"][name]
    assert stat["successes"] == successes
    assert stat["opportunities"] == opportunities
    assert stat["posterior_mean"] == pytest.approx((successes + 1) / (opportunities + 2))
    low, high = stat["interval"]
    assert 0 <= low <= stat["posterior_mean"] <= high <= 1
    assert stat["interval_method"]
    assert stat["model_version"]


def _assert_action_posteriors(player):
    for action_group in player["actions"]:
        assert isinstance(action_group["context"], str) and action_group["context"]
        support = action_group["legal_actions"]
        counts = action_group["counts"]
        probabilities = action_group["probabilities"]
        assert len(support) == len(set(support))
        assert set(support) == set(counts) == set(probabilities)
        assert sum(counts.values()) == action_group["observations"]
        assert action_group["action_encoding"] == "family"
        assert sum(probabilities.values()) == pytest.approx(1)
        for action in support:
            assert probabilities[action] == pytest.approx(
                (counts[action] + 1) / (action_group["observations"] + len(support))
            )
            assert not action.startswith("raise_to:")


def test_fold_and_forced_blinds_are_not_voluntary_preflop_actions():
    actions = (*PRIVATE_DEAL, "fold")
    result = summarize_profiles(HoldemGame((20, 20)), actions, session_id="fold-session")
    assert result["schema_version"] == "1.1"
    assert result["session_id"] == "fold-session"
    assert result["state_version"] == len(actions)
    assert isinstance(result["warnings"], list)
    for seat in (0, 1):
        player = _player(result, seat)
        _assert_stat(player, "VPIP", 0, 1)
        _assert_stat(player, "PFR", 0, 1)
        _assert_action_posteriors(player)
    assert sum(group["observations"] for group in _player(result, 0)["actions"]) == 1
    assert _player(result, 1)["actions"] == []


def test_all_in_raise_and_call_have_one_hand_denominator_each():
    result = summarize_profiles(HoldemGame((20, 20)), ALL_IN, session_id="all-in-session")
    for seat in (0, 1):
        player = _player(result, seat)
        _assert_stat(player, "VPIP", 1, 1)
        _assert_stat(player, "PFR", int(seat == 0), 1)
        _assert_action_posteriors(player)
        assert sum(group["observations"] for group in player["actions"]) == 1


def test_big_blind_check_is_not_vpip_and_repeated_checks_are_action_opportunities():
    actions = (
        *PRIVATE_DEAL,
        "call",
        "check",
        *RUNOUT[:3],
        "check",
        "check",
        RUNOUT[3],
        "check",
        "check",
        RUNOUT[4],
        "check",
        "check",
    )
    result = summarize_profiles(HoldemGame((20, 20)), actions, session_id="checks")
    for seat in (0, 1):
        player = _player(result, seat)
        _assert_stat(player, "VPIP", int(seat == 0), 1)
        _assert_stat(player, "PFR", 0, 1)
        assert sum(group["observations"] for group in player["actions"]) == 4
        _assert_action_posteriors(player)


@pytest.mark.parametrize("cutoff", [0, 1, 3, 4, 5, 6, 9, 10])
def test_unfinished_hand_never_uses_future_vpip_or_pfr(cutoff):
    game = HoldemGame((20, 20))
    result = summarize_profiles(game, ALL_IN, session_id="prefix", as_of=cutoff)
    assert result == summarize_profiles(game, ALL_IN[:cutoff], session_id="prefix")
    assert result["state_version"] == cutoff
    for player in result["players"]:
        _assert_stat(player, "VPIP", 0, 0)
        _assert_stat(player, "PFR", 0, 0)


def test_future_invalid_events_do_not_affect_a_valid_past_prefix():
    game = HoldemGame((20, 20))
    prefix = ALL_IN[:5]
    expected = summarize_profiles(game, prefix, session_id="past")
    assert (
        summarize_profiles(
            game, (*prefix, "impossible-future-action", "deal:As"), session_id="past", as_of=5
        )
        == expected
    )


def test_private_opponent_cards_and_future_showdown_do_not_change_profiles():
    game = HoldemGame((20, 20))
    original = summarize_profiles(game, ALL_IN, session_id="private")
    alternative = ("deal:Qs", "deal:As", "deal:Qh", "deal:Ah", *ALL_IN[4:])
    assert summarize_profiles(game, alternative, session_id="private") == original
    serialized = json.dumps(original, ensure_ascii=False)
    for card in ("Ks", "Kh", "As", "Ah", "2c", "3d", "4h", "7s", "9c"):
        assert card not in serialized


def test_legal_off_tree_raise_sizes_share_the_raise_action_family():
    game = HoldemGame((100, 100))
    results = [
        summarize_profiles(game, (*PRIVATE_DEAL, f"raise_to:{size}"), session_id="sizes")
        for size in (4, 5, 7)
    ]
    for result in results:
        player = _player(result, 0)
        _assert_action_posteriors(player)
        assert len(player["actions"]) == 1
        assert player["actions"][0]["counts"]["raise_to"] == 1
    assert all(
        _player(result, 0)["actions"] == _player(results[0], 0)["actions"] for result in results
    )


@pytest.mark.parametrize(
    ("game", "actions"),
    [
        (KuhnPoker(), ("deal:0,2", "bet", "call")),
        (LeducPoker(), ("deal:0,2", "check", "check", "board:4", "bet", "fold")),
    ],
)
def test_toy_games_only_report_supported_action_statistics(game, actions):
    result = summarize_profiles(game, actions, session_id="toy")
    assert result["state_version"] == len(actions)
    for player in result["players"]:
        assert "VPIP" not in player.get("statistics", {})
        assert "PFR" not in player.get("statistics", {})
        _assert_action_posteriors(player)
    assert sum(
        group["observations"] for player in result["players"] for group in player["actions"]
    ) == (2 if game.game_id == "kuhn" else 4)


def test_replay_is_idempotent_and_json_serializable():
    game = HoldemGame((20, 20))
    first = summarize_profiles(game, ALL_IN, session_id="repeat")
    second = summarize_profiles(game, ALL_IN, session_id="repeat")
    assert first == second
    assert json.loads(json.dumps(first, allow_nan=False)) == first


@pytest.mark.parametrize("as_of", [-1, len(ALL_IN) + 1, 1.5, True, "2"])
def test_invalid_replay_cutoff_rejected(as_of):
    with pytest.raises(ValueError):
        summarize_profiles(HoldemGame((20, 20)), ALL_IN, session_id="invalid", as_of=as_of)


@pytest.mark.parametrize(
    "actions",
    [
        (*PRIVATE_DEAL, "check"),
        (*PRIVATE_DEAL, "raise_to:3"),
        ("deal:As", "deal:As"),
        (*PRIVATE_DEAL, "fold", "call"),
    ],
)
def test_illegal_past_actions_rejected(actions):
    with pytest.raises(ValueError):
        summarize_profiles(HoldemGame((20, 20)), actions, session_id="invalid")
