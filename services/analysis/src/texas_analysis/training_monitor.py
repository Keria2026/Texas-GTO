"""Read-only progress and synthetic public-state probes of the installed policy."""

import sqlite3
from contextlib import closing
from dataclasses import fields
from random import Random
from threading import Lock
from time import monotonic, time
from types import SimpleNamespace

from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.mushroom_public import build_public_query, lookup_public_policy
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.training_store import _header

from .capture_recommendation import _actions, _search


class TrainingMonitor:
    def __init__(self, path):
        self.path = path
        self._lock = Lock()
        self._cache = None
        self._cached_at = 0

    def status(self):
        with self._lock:
            if self._cache is not None and monotonic() - self._cached_at < 60:
                return self._cache
            if not self.path.is_file():
                return {"available": False, "reason": "尚无已保存的训练模型"}
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA cache_size=-4096")
                db.execute("BEGIN")
                document = _header(db, full_check=False)
                game = MushroomPopulationGame.from_context(document["training_context"])
                rows = db.execute(
                    "SELECT stage,count(*),sum(has_average) FROM training_entries "
                    "WHERE substr(information_key,1,?)=? GROUP BY stage",
                    (len(game.training_key_prefix), game.training_key_prefix),
                ).fetchall()
                feedback = db.execute(
                    "SELECT count(*) FROM training_entries WHERE stage<4 AND has_average=1 "
                    "AND substr(information_key,1,?)=? "
                    "AND EXISTS (SELECT 1 FROM json_each(regrets) WHERE value!=0)",
                    (len(game.training_key_prefix), game.training_key_prefix),
                ).fetchone()[0]
            meta = document["metadata"]
            config = meta["pluribus"]
            iteration = meta["iterations"]
            migration = game.rule_migration
            interval, after = config["snapshot_interval"], config["average_after"]
            self._cache = {
                "available": True,
                "first_hand_average_with_regret": feedback,
                "sampled_at": time(),
                "iterations": iteration,
                "rule_iterations": iteration - (migration["source_iterations"] if migration else 0),
                "information_sets": sum(r[1] for r in rows),
                "averaged_information_sets": sum(r[2] for r in rows),
                "stages": [{"stage": s, "total": n, "averaged": a} for s, n, a in rows],
                "snapshot_interval": interval,
                "next_snapshot": after + (max(0, iteration - after) // interval + 1) * interval,
                "average_samples": config.get("average_samples", 1),
                "training_roots": len(game.roots),
                "observed_openings": game.observed_opening_count,
                "bytes": sum(
                    p.stat().st_size
                    for p in (self.path, self.path.with_name(self.path.name + "-wal"))
                    if p.exists()
                ),
                "namespace": game.training_key_prefix,
            }
            self._cached_at = monotonic()
            return self._cache


def _sample_public_case(population, seed, street):
    rng = Random(seed)
    # A real eight-player query starts in hand one; never test the continuation as a live hand.
    roots = [i for i, root in enumerate(population.roots) if not root["waiting_for_top_up"]]
    if not roots:
        raise ValueError("No eight-player openings in the current model")
    for _ in range(12):
        index = rng.choice(roots)
        model = population._game(index)
        state = model.initial_state()
        if len(state.underlying.table.participants) != 8:
            continue
        # Random private deals and legal histories, independent of the policy being tested.
        skip = rng.randrange(8)
        for _ in range(160):
            actor = model.actor(state)
            if actor is None or state.underlying.table.hand_number != 1:
                break
            if actor == -1:
                action, _ = model.sample_chance_outcome(state, rng)
            else:
                stage = model.betting_round(state)
                if stage == street and skip == 0:
                    return model, state, index
                if stage > street:
                    break
                if stage == street:
                    skip -= 1
                options = model.legal_actions(state)
                passive = next((a for a in ("check", "call") if a in options), options[0])
                action = passive if rng.random() < 0.8 else rng.choice(options)
            state = model.transition(state, action)
    raise ValueError("本次随机路径未到达目标街道，请更换种子再试")


def public_facts(model, state):
    """Drop all opponents' private cards before constructing the production query."""
    table = state.underlying.table
    engine, inner = table.hand.game, table.hand.state
    public_game = PublicHoldemGame(
        HoldemRules(
            engine.initial_stacks,
            small_blind=engine.small_blind,
            big_blind=engine.big_blind,
            straddle=engine.straddle,
            ante=engine.ante,
            button=engine.button,
        )
    )
    values = {
        f.name: getattr(inner, f.name)
        for f in fields(PublicHoldemState)
        if f.name not in ("rules_version", "awaiting_settlement")
    }
    public = PublicHoldemState(
        **values, rules_version=public_game.rules_version, awaiting_settlement=inner.terminal
    )
    fees = dict(table.ledger.snapshot().outstanding_contributions)
    return dict(
        model=model,
        hand_game=public_game,
        public_state=public,
        table_button=table.button,
        policy_anchor=model.base_game.button,
        hand_offset=0,
        participants=table.participants,
        stacks=table.stacks,
        outstanding=tuple(fees.get(str(seat), 0) for seat in range(8)),
        pool=table.pool,
        waiting_for_top_up=table.waiting_for_top_up,
        hero_seat=table.participants[inner.to_act],
        hero_cards=inner.hole_cards[inner.to_act],
    )


def simulate(path, *, seed, street, search):
    started = monotonic()
    if not path.is_file():
        raise ValueError("尚无训练模型")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        document = _header(db, full_check=False)
    population = MushroomPopulationGame.from_context(document["training_context"])
    model, state, root = _sample_public_case(population, seed, street)
    facts = public_facts(model, state)
    query = build_public_query(**facts)
    public = facts["public_state"]
    players = tuple("hero" if i == facts["hero_seat"] else f"player-{i}" for i in range(8))
    replay = SimpleNamespace(game=facts["hand_game"], state=public, player_ids=players)
    result = {
        "synthetic": True,
        "seed": seed,
        "root": root,
        "street": street,
        "hero_seat": facts["hero_seat"] + 1,
        "hero_cards": list(facts["hero_cards"]),
        "board": list(public.board),
        "stacks": list(public.stacks),
        "pot": public.pot,
        "mushroom_pool": facts["pool"],
        "to_call": replay.game.amount_to_call(public),
        "history": [
            {"seat": e.player + 1, "street": e.street, "action": e.action, "amount": e.amount_delta}
            for e in public.history
        ],
        "status": "unavailable",
        "reason": "strategy_not_trained",
        "actions": [],
        "selected_action": None,
        "search": None,
    }
    with policy_snapshot(model, path, missing="error") as policy:
        if policy.metadata["algorithm"] != "pluribus":
            raise ValueError("Requires a Pluribus model")
        entry = lookup_public_policy(policy, query)
        result["iterations"] = policy.iterations
        result["blueprint_hit"] = entry is not None
        solved = None
        if search:
            solved, result["search"] = _search(
                model,
                replay,
                query,
                policy,
                facts["hero_cards"],
                seed=seed,
                seconds=1.5,
                should_cancel=lambda: False,
                pr_context={"previous": []},
                baseline=entry,
            )
        if solved is not None:
            entry = solved
        if entry is not None:
            if solved is None and policy.is_uniform_without_regrets(
                query.player, query.information_key
            ):
                result["reason"] = "strategy_feedback_unavailable"
            else:
                actions = _actions(
                    replay,
                    query,
                    entry.probabilities,
                    {"legal_actions": ["FOLD", "CHECK", "CALL", "BET", "RAISE", "ALL_IN"]},
                )
                selected = Random(seed ^ 0xA17E).choices(range(len(actions)), entry.probabilities)[
                    0
                ]
                result.update(
                    status="ready",
                    reason="searched_strategy" if solved else "covered_blueprint",
                    actions=actions,
                    selected_action=selected,
                )
    result["elapsed_ms"] = round((monotonic() - started) * 1000)
    return result
