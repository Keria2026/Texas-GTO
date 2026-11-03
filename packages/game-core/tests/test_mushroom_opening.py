"""Existing escrow is owned starting wealth, never another fee or free refund."""

from hashlib import sha256
from random import Random

import pytest

from texas_game_core.mushroom import MushroomEvent, MushroomLedger
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_game_core.mushroom_table import MushroomTable


def deal(game, state, seed=19):
    rng = Random(seed)
    while game.actor(state) == -1:
        action, _ = game.sample_chance_outcome(state, rng)
        state = game.transition(state, action)
    return state


def finish_by_folding(table, *, small_blind_wins=False, seed=19):
    table.deal_to_decision(rng=Random(seed))
    while not table.hand.state.terminal:
        actor = table.hand.game.actor(table.hand.state)
        actions = table.hand.game.legal_actions(table.hand.state)
        if small_blind_wins and actor == table.hand.game.small_blind_player:
            action = next(action for action in actions if action.startswith("raise_to:"))
        else:
            action = "fold"
        table.transition(action)


def financial_state(table):
    return (
        table.stacks,
        table.pool,
        table.ledger.snapshot().outstanding_contributions,
        table.button,
        table.participants,
        table.waiting_for_top_up,
        table.ended,
        table.funding_total,
    )


def test_seeded_ledger_refunds_once_and_fork_rebuild_keep_the_same_opening():
    opening = {"0": 70, "1": 30, "2": 0}
    ledger = MushroomLedger(MushroomTable((800,) * 4).ledger.rules, opening_contributions=opening)
    opening["0"] = 999
    initial = ledger.snapshot()
    assert initial.pool == 100 and initial.player_net == (("0", -70), ("1", -30))
    assert initial.outstanding_contributions == (("0", 70), ("1", 30))
    assert initial.check_conservation() and ledger.events == ()
    ledger.begin_hand(
        "begin", "1", ("0", "1", "2", "3"), "1", dict.fromkeys("0123", 800), timing="before_hand"
    )
    ledger.settle_hand("settle", "1", None, outcome="split_main_pot")
    fork = ledger.fork()
    refund = fork.end_session("end")
    assert dict(refund.transfers) == {"0": 90, "1": 70, "2": 20, "3": 20}
    assert refund.state.pool == 0 and refund.state.check_conservation()
    assert fork.end_session("end").transfers == ()
    assert ledger.snapshot().pool == 200 and not ledger.snapshot().ended
    assert fork.rebuild(fork.events) == refund.state
    with pytest.raises(ValueError, match="Unknown mushroom event"):
        fork.rebuild((MushroomEvent("bad", "invalid"),))
    assert fork.snapshot() == refund.state
    assert fork.rebuild(()) == initial


@pytest.mark.parametrize(
    "opening", [[("0", 10)], {"": 10}, {1: 10}, {"0": -1}, {"0": True}, {"0": 1.5}]
)
def test_invalid_opening_ledger_contributions_are_rejected(opening):
    with pytest.raises(ValueError):
        MushroomLedger(MushroomTable().ledger.rules, opening_contributions=opening)


def test_opening_escrow_does_not_debit_cash_and_only_current_hand_fees_are_added():
    paid = (20, 60, 40, 20, 0, 0, 0, 0)
    table = MushroomTable(outstanding=paid)
    assert table.stacks == (800,) * 8 and table.pool == 140
    assert table.funding_total == 6540
    table.begin_hand()
    assert table.hand.opening_pool == 140 and table.pool == 320
    assert table.hand.starting_stacks == (800,) * 8
    assert dict(table.ledger.snapshot().outstanding_contributions) == {
        str(seat): paid[seat] + (40 if seat == 1 else 20) for seat in range(8)
    }
    assert sum(table.stacks) + table.hand.state.pot == 6400 - 180
    table.validate()


def test_fewer_than_four_eligible_refunds_existing_owners_without_new_fees_or_profit():
    paid = (60, 20, 40, 80)
    game = MushroomSessionGame((800, 800, 800, 40), outstanding=paid, waiting_for_top_up=(3,))
    state = game.initial_state()
    assert state.table.ended and state.table.hand_number == 0 and state.table.pool == 0
    assert state.table.stacks == (860, 820, 840, 120)
    assert game.initial_wealth == state.table.stacks
    assert game.terminal_payoffs(state) == (0.0,) * 4
    state.table.end_session()
    assert state.table.stacks == game.initial_wealth


def test_waiting_seat_is_not_readmitted_by_a_changed_button_without_explicit_top_up():
    stacks = (800, 40, 800, 800, 800, 800, 800, 800)
    table = MushroomTable(
        stacks, button=3, outstanding=(0, 60, 0, 0, 0, 0, 0, 0), waiting_for_top_up=(1,)
    )
    table.begin_hand()
    assert 1 not in table.participants and table.stacks[1] == 40
    assert dict(table.ledger.snapshot().outstanding_contributions)["1"] == 60
    finish_by_folding(table)
    table.top_up(1, 360)
    assert table.waiting_for_top_up == () and table.stacks[1] == 400
    table.begin_hand()
    assert 1 in table.participants and table.hand.starting_stacks[1] == 400
    assert table.funding_total == sum(stacks) + 60 + 360


@pytest.mark.parametrize("factory", [MushroomTable, MushroomSessionGame])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"outstanding": (0,) * 7},
        {"outstanding": (0,) * 7 + (-1,)},
        {"outstanding": (0,) * 7 + (True,)},
        {"outstanding": (0,) * 7 + (1.5,)},
        {"outstanding": {str(i): 0 for i in range(8)}},
        {"waiting_for_top_up": (1, 0)},
        {"waiting_for_top_up": (1, 1)},
        {"waiting_for_top_up": (8,)},
        {"waiting_for_top_up": (True,)},
        {"waiting_for_top_up": None},
    ],
)
def test_invalid_opening_vectors_are_rejected(factory, kwargs):
    with pytest.raises(ValueError):
        factory(**kwargs)


@pytest.mark.parametrize("stacks", [(800,) * 8, (800, 40, 39, 800, 800, 800, 800, 800)])
@pytest.mark.parametrize("small_blind_wins", [False, True])
def test_new_window_matches_actual_carried_ledger_and_end_balances(stacks, small_blind_wins):
    original = MushroomTable(stacks)
    original.begin_hand()
    finish_by_folding(original)
    assert original.pool > 0
    paid = dict(original.ledger.snapshot().outstanding_contributions)
    resumed = MushroomTable(
        original.stacks,
        button=original.button,
        outstanding=tuple(paid.get(str(seat), 0) for seat in range(8)),
        waiting_for_top_up=original.waiting_for_top_up,
    )
    original.begin_hand()
    resumed.begin_hand()
    assert financial_state(original) == financial_state(resumed)
    assert original.hand.state == resumed.hand.state
    for table in (original, resumed):
        finish_by_folding(table, small_blind_wins=small_blind_wins, seed=23)
        table.end_session()
        table.validate()
    assert financial_state(original) == financial_state(resumed)
    assert original.hand.state == resumed.hand.state
    assert sum(resumed.stacks) == sum(stacks)


def test_game_refunds_are_starting_wealth_and_awarded_escrow_is_not_refunded_again():
    paid = (20, 60, 40, 20, 0, 0, 0, 0)
    for award in (False, True):
        game = MushroomSessionGame(outstanding=paid, horizon=1)
        state = deal(game, game.initial_state())
        actions = ["fold"] * 5 + ["raise_to:16", "fold", "fold"] if award else ["fold"] * 7
        for action in actions:
            state = game.transition(state, action)
        values = game.terminal_payoffs(state)
        assert sum(values) == 0 and state.table.pool == 0
        assert state.table.ledger.snapshot().outstanding_contributions == ()
        if award:
            assert values == (-40.0, 232.0, -64.0, -48.0, -20.0, -20.0, -20.0, -20.0)
            assert state.table.stacks[1] == 1092
        else:
            assert values == (0.0, -2.0, -4.0, 6.0, 0.0, 0.0, 0.0, 0.0)
        before = state.table.stacks
        state.table.end_session()
        assert state.table.stacks == before


def test_full_model_rejects_another_opening_and_mutated_funding():
    game = MushroomSessionGame(outstanding=(20,) * 8, waiting_for_top_up=(1,))
    state = game.initial_state()
    with pytest.raises(ValueError, match="another mushroom"):
        MushroomSessionGame().actor(state)
    state.table.funding_total += 1
    with pytest.raises(ValueError, match="external funding"):
        game.actor(state)


@pytest.mark.parametrize(
    "players,button,digests",
    [
        (
            8,
            0,
            (
                "52d87a261cc3cfdef4de66b88c6bd704d6cddc6b0043d76dccae7b7aa0282e43",
                "584adf12df75d7ad3406103fc53c4d99358dde724712954ae926dda6c7a7c309",
                "1c4e00e6debfeed82c98d883d3b3c66c6ecd6b7b88416b4b6adea49c6cb0e07b",
            ),
        ),
        (
            4,
            2,
            (
                "136bc7ed470737a316450ef5d0113da2a047807705697893b33749d4c8fbd655",
                "8d0eff1ed549e19a45404de85f104a3e48c694cb4ab5449974912fdbb71b0b94",
                "3b59e394f8ea88ce42f52d4f537087948ab14f7ee68f0d8a30506866065bb1c4",
            ),
        ),
    ],
)
def test_zero_opening_preserves_prechange_rules_public_and_private_keys(players, button, digests):
    # Frozen before this change; explicit zero anchors must equal the legacy default.
    for opening in ({}, {"outstanding": (0,) * players, "waiting_for_top_up": ()}):
        game = MushroomSessionGame((800,) * players, button=button, **opening)
        state = deal(game, game.initial_state())
        actual = (
            game.rules_version,
            game.public_key(state),
            game.information_key(state, game.actor(state)),
        )
        assert tuple(sha256(value.encode()).hexdigest() for value in actual) == digests
