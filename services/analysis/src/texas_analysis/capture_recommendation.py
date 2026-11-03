"""State-bound eight-seat policies and bounded full-range postflop search."""

import hashlib
import json
from datetime import UTC, datetime
from time import monotonic

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.behavior_policy import BehaviorPolicy, compare_actions
from texas_strategy.evaluation import CancellationError, EvaluationLimitError, TraversalBudget
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_belief import MushroomFirstHandRoot, MushroomStreetRoot
from texas_strategy.mushroom_public import build_public_query, lookup_public_policy
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.policy import PolicyEntry, TabularPolicy
from texas_strategy.search import DepthLimitedGame, SearchCoverageError

from .capture_ledger import derive_mushroom_ledger
from .capture_replay import ReplayUnavailable, replay_capture

CAPABILITY = "capture_recommendations_v1"
SEARCH_CAPABILITY = "capture_search_v1"
PR_CAPABILITY = "capture_pr_v1"
SCHEMA = "tgto.capture-recommendation/0.1"
FRESHNESS_MS = 15_000
LIFETIME_MS = 10_000
QUERY_SECONDS = 0.5
SEARCH_SECONDS = 1.5
SEARCH_NODES = 30_000


class RecommendationUnavailable(ValueError):
    pass


def prepare_query(state, prefix, *, ledger_resolver=None):
    """Bind a rolling two-hand approximation to observed cash and mushroom rights."""
    if state["gap"] or state["closed"] or not state["history_complete"]:
        raise RecommendationUnavailable("incomplete_action_history")
    if state["turn"] is None or state["turn"]["player_id"] != "hero":
        raise RecommendationUnavailable("not_hero_turn")
    if None in state["hero_cards"]:
        raise RecommendationUnavailable("hero_cards_unavailable")
    replay = replay_capture(prefix)
    if any(state[key] != value for key, value in prefix[-1]["data"].items()):
        raise RecommendationUnavailable("public_state_mismatch")
    if replay.seats != tuple(range(1, 9)) or len(state["players"]) != 8:
        raise RecommendationUnavailable("requires_eight_participants")
    if replay.hand_id != state["hand_id"]:
        raise RecommendationUnavailable("hand_changed")
    if "hero" not in replay.player_ids or replay.pending_returns != (0,) * 8:
        raise RecommendationUnavailable("public_state_incomplete")
    try:
        ledger = (
            ledger_resolver(hand_id=state["hand_id"], fees=replay.fees)
            if ledger_resolver is not None
            else derive_mushroom_ledger(
                hand_id=state["hand_id"], fees=replay.fees, settlements=state["settlements"]
            )
        )
    except ValueError as error:
        raise RecommendationUnavailable("mushroom_ledger_unavailable") from error
    if set(ledger["outstanding"]) - set(replay.player_ids):
        raise RecommendationUnavailable("unmapped_mushroom_owner")
    outstanding = tuple(ledger["outstanding"].get(p, 0) for p in replay.player_ids)
    opening_rights = tuple(outstanding[i] - replay.fees[p] for i, p in enumerate(replay.player_ids))
    # With all eight seats paid, the predecessor of the observed SB is unambiguous.
    button = replay.game.button
    model = MushroomContextGame(
        MushroomSessionGame(
            stacks=replay.opening_cash,
            button=button,
            horizon=2,
            outstanding=opening_rights,
            ante=replay.game.ante,
        )
    )
    query = build_public_query(
        model=model,
        hand_game=replay.game,
        public_state=replay.state,
        table_button=button,
        policy_anchor=button,
        hand_offset=0,
        participants=tuple(range(8)),
        stacks=replay.state.stacks,
        outstanding=outstanding,
        pool=ledger["pool"],
        waiting_for_top_up=(),
        hero_seat=replay.player_ids.index("hero"),
        hero_cards=tuple(state["hero_cards"]),
    )
    return model, replay, query


def _actions(replay, query, probabilities, turn):
    actor, state = replay.game.actor(replay.state), replay.state
    options = set(turn["legal_actions"])
    result = []
    for label, concrete, probability in zip(
        query.actions, query.concrete_actions, probabilities, strict=True
    ):
        target = None
        if concrete.startswith("raise_to:"):
            target = int(concrete.split(":")[1])
            amount = target - state.street_bets[actor]
            kind = (
                "ALL_IN"
                if amount == state.stacks[actor]
                else ("BET" if state.current_bet == 0 else "RAISE")
            )
        else:
            amount = (
                min(replay.game.amount_to_call(state), state.stacks[actor])
                if concrete == "call"
                else 0
            )
            kind = concrete.upper()
        allowed = {kind}
        if amount == state.stacks[actor] and amount > 0:
            allowed.add("ALL_IN")
            if target is not None:
                allowed.add("BET" if state.current_bet == 0 else "RAISE")
        if not options.intersection(allowed):
            raise RecommendationUnavailable("client_legal_actions_conflict")
        result.append(
            {
                "action": kind,
                "label": label,
                "amount_delta": amount,
                "raise_to": target,
                "probability": probability,
            }
        )
    return result


def _search(
    model,
    replay,
    query,
    policy,
    hero_cards,
    *,
    seed,
    seconds,
    should_cancel,
    pr_context=None,
    baseline=None,
):
    started = monotonic()
    budget = TraversalBudget(
        max_nodes=SEARCH_NODES, deadline_seconds=seconds, should_cancel=should_cancel
    )
    try:
        root = (
            MushroomFirstHandRoot(model, replay.state, policy, budget=budget)
            if pr_context is None
            else MushroomStreetRoot(
                model, replay.state, policy, budget=budget, previous=pr_context["previous"]
            )
        )
        adapted = isinstance(policy, BehaviorPolicy)
        solved = root.solve(
            replay.player_ids.index("hero"),
            tuple(hero_cards),
            iterations=100,
            seed=seed,
            max_nodes=SEARCH_NODES,
            max_rollout_nodes=SEARCH_NODES,
            **({"search_seconds": seconds * 0.6} if adapted else {}),
        )
        budget.check_time()
        if solved.actions != query.actions or solved.blueprint_fallback_queries:
            raise ValueError("Search actions or continuation coverage do not match the query")
        entry = PolicyEntry(solved.actions, solved.probabilities)
        detail = {
            "status": "complete",
            "iterations": solved.diagnostics.completed_iterations,
            "stop_reason": solved.diagnostics.stop_reason,
            "nodes": solved.diagnostics.nodes,
            "rollout_nodes": solved.subgame.rollout_budget.nodes,
            "root_draws": solved.subgame.root_draws,
        }
        if adapted and policy.applied:
            if baseline is None:
                entry, detail = None, {"status": "profile_rejected"}
            else:
                mixture = PolicyEntry(
                    entry.actions,
                    tuple(
                        0.75 * base + 0.25 * improved
                        for base, improved in zip(
                            baseline.probabilities, entry.probabilities, strict=True
                        )
                    ),
                )
                evaluation = compare_actions(
                    model,
                    root.target_sampler(replay.player_ids.index("hero"), tuple(hero_cards)),
                    policy,
                    query.player,
                    baseline,
                    mixture,
                    budget=budget,
                    seed=seed ^ 0xA17E53,
                )
                pr_context["evaluation"] = evaluation
                if evaluation["accepted"]:
                    entry = mixture
                    pr_context["adapted"] = True
                else:
                    entry, detail = None, {"status": "profile_rejected"}
        if pr_context is not None and entry is not None:
            pr_context["target_depth"] = root.target_depth
            # Bound transient per-hand caches; missing saved rows still require blueprint coverage.
            if (
                len(solved.policy.entries) + len(solved.average_policy.entries) <= 4000
                and sum(
                    len(key[1]) + len(row.actions) * 96
                    for table in (solved.policy, solved.average_policy)
                    for key, row in table.entries.items()
                )
                <= 4 * 1024 * 1024
            ):
                entries = dict(solved.policy.entries)
                entries[
                    query.player,
                    DepthLimitedGame.root_information_key(
                        solved.information_key, root.target_depth
                    ),
                ] = entry
                pr_context["record"] = {
                    "street": replay.state.street,
                    "root_history": root.root_history,
                    "current": TabularPolicy(entries, missing="error"),
                    "average": solved.average_policy,
                }
    except SearchCoverageError:
        entry, detail = None, {"status": "coverage_missing"}
    except EvaluationLimitError:
        entry, detail = None, {"status": "budget"}
    detail["elapsed_ms"] = int((monotonic() - started) * 1000)
    return entry, detail


def recommend(
    *,
    source,
    through,
    state,
    prefix,
    policy_path,
    now_ms=None,
    should_cancel=None,
    ledger_resolver=None,
    search=False,
    pr_context=None,
    opponent_model=None,
):
    """One short committed policy snapshot; timestamps also reject queued old hands."""
    started = monotonic()

    def check_cancelled():
        if should_cancel is not None and should_cancel():
            raise CancellationError("Observation changed; no recommendation available")

    check_cancelled()
    now_ms = int(datetime.now(UTC).timestamp() * 1000) if now_ms is None else now_ms
    result = {
        "schema": SCHEMA,
        "type": "capture.recommendation",
        "source_id": source,
        "through_sequence": through,
        "hand_id": state["hand_id"],
        "status": "unavailable",
        "reason": "public_state_incomplete",
        "actions": [],
        "selected_action": None,
        "model": None,
        "issued_at_ms": now_ms,
        "expires_at_ms": now_ms,
        "elapsed_ms": 0,
        "warnings": [],
    }
    if pr_context is not None:
        result["pr"] = {
            "release": "Pre1.0",
            "search_root": "street_start",
            "target_depth": None,
            "opponent_version": opponent_model["version"] if opponent_model else None,
            "adaptation_weight": 0.0,
            "evaluation": None,
        }
    try:
        if not prefix or prefix[-1]["sequence"] != through:
            raise RecommendationUnavailable("public_state_incomplete")
        captured = int(datetime.fromisoformat(prefix[-1]["captured_at"]).timestamp() * 1000)
        if not -5000 <= now_ms - captured < FRESHNESS_MS:
            raise RecommendationUnavailable("observation_expired")
        model, replay, query = prepare_query(state, prefix, ledger_resolver=ledger_resolver)
        check_cancelled()
        search = search and (replay.state.street > 0 or pr_context is not None)
        deadline = started + QUERY_SECONDS + (SEARCH_SECONDS if search else 0)
        if policy_path is None or not policy_path.is_file():
            raise RecommendationUnavailable("model_unavailable")
        with policy_snapshot(model, policy_path, missing="error") as policy:
            metadata = policy.metadata
            if metadata["algorithm"] != "pluribus":
                raise RecommendationUnavailable("requires_pluribus_model")
            entry = lookup_public_policy(policy, query)
            check_cancelled()
            version = policy.metadata_digest
            identity = json.dumps([source, state["hand_id"], query.information_key, version])
            search_policy = policy
            if pr_context is not None and opponent_model is not None:
                search_policy = BehaviorPolicy(
                    policy,
                    opponent_model["counts"],
                    source=source,
                    players=replay.player_ids,
                    hero=replay.player_ids.index("hero"),
                    button=replay.game.button,
                )
                identity += opponent_model["version"]
            detail, strategy_source = None, "pluribus_blueprint"
            if search:
                remaining = min(SEARCH_SECONDS, deadline - monotonic() - 0.05)
                if remaining <= 0:
                    raise RecommendationUnavailable("query_timeout")
                solved, detail = _search(
                    model,
                    replay,
                    query,
                    search_policy,
                    state["hero_cards"],
                    seed=int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8]),
                    seconds=remaining,
                    should_cancel=check_cancelled,
                    **(
                        {"pr_context": pr_context, "baseline": entry}
                        if pr_context is not None
                        else {}
                    ),
                )
                check_cancelled()
                if solved is not None:
                    entry, strategy_source = (
                        solved,
                        (
                            "profile_search"
                            if pr_context and pr_context.get("adapted")
                            else "pluribus_search"
                        ),
                    )
            if entry is None:
                raise RecommendationUnavailable("strategy_not_trained")
            if strategy_source == "pluribus_blueprint" and policy.is_uniform_without_regrets(
                query.player, query.information_key
            ):
                raise RecommendationUnavailable("strategy_feedback_unavailable")
            actions = _actions(replay, query, entry.probabilities, state["turn"])
            if pr_context is not None:
                record = pr_context.setdefault(
                    "record",
                    {
                        "street": replay.state.street,
                        "root_history": sum(
                            e.street != replay.state.street_name for e in replay.state.history
                        ),
                        "current": TabularPolicy(missing="error"),
                        "average": TabularPolicy(missing="error"),
                    },
                )
                record["published"] = TabularPolicy(
                    {(query.player, query.information_key): entry}, missing="error"
                )
            model_info = {
                "source": strategy_source,
                "version": version,
                "iterations": policy.iterations,
                "rules_version": model.rules_version,
                "quality": "finite_horizon_unvalidated",
                "horizon_hands": 2,
                "opponent_adaptation": strategy_source == "profile_search",
            }
            if detail is not None:
                model_info["search"] = detail
        if monotonic() >= deadline:
            raise RecommendationUnavailable("query_timeout")
        # Keep the same random quantile; a newly computed search distribution may still differ.
        draw = (int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8]) >> 11) / 2**53
        selected = max(i for i, action in enumerate(actions) if action["probability"] > 0)
        for index, action in enumerate(actions):
            draw -= action["probability"]
            if draw < 0:
                selected = index
                break
        elapsed = int((monotonic() - started) * 1000)
        expires = min(now_ms + LIFETIME_MS, captured + FRESHNESS_MS)
        if now_ms + elapsed >= expires:
            raise RecommendationUnavailable("observation_expired")
        result.update(
            status="ready",
            reason="searched_strategy"
            if strategy_source != "pluribus_blueprint"
            else "covered_blueprint",
            actions=actions,
            selected_action=selected,
            model=model_info,
            expires_at_ms=expires,
            warnings=[
                "two_hand_horizon",
                "bucketed_cards_and_chips",
                "quality_not_certified",
                "client_raise_bounds_not_interpreted",
            ]
            + (
                ["local_search_not_equilibrium_certified", "latest_completed_search_policy"]
                if strategy_source != "pluribus_blueprint"
                else []
            )
            + (
                ["profile_model_not_equilibrium_certified"]
                if strategy_source == "profile_search"
                else ["opponent_profiles_not_applied"]
            ),
        )
    except (RecommendationUnavailable, ReplayUnavailable) as error:
        result["reason"] = str(error)
    except (ValueError, OSError):
        result["reason"] = "model_or_state_invalid"
    result["elapsed_ms"] = int((monotonic() - started) * 1000)
    if pr_context is not None:
        result["pr"].update(
            target_depth=pr_context.get("target_depth"),
            adaptation_weight=0.25
            if result["status"] == "ready" and pr_context.get("adapted")
            else 0.0,
            evaluation=pr_context.get("evaluation"),
        )
    check_cancelled()
    return result
