"""Bounded, deterministic observation projection. Never deals cards or chooses actions."""

from copy import deepcopy

from .capture_contract import STATE_SCHEMA, unique_cards


def empty_state():
    return {
        "table_id": None,
        "hand_id": None,
        "street": "unknown",
        "hero_cards": [None, None],
        "board": [],
        "players": [],
        "actions": [],
        "turn": None,
        "history_complete": False,
        "closed": False,
        "rules_version": "wpk-observed-unverified-v1",
        "gap": False,
        "settlements": [],
        "pending_settlements": [],
    }


def reduce_event(state, event):
    s = deepcopy(state) if state else empty_state()
    d, kind = event.data, event.kind
    if kind in ("settlement", "settlement_pending"):
        # Result/log packets for the previous hand may arrive during the next hand.
        key = event.hand_id
        if kind == "settlement":
            old = next((r for r in s["settlements"] if r["hand_id"] == key), None)
            result = {"hand_id": key, **d}
            if old is not None and old != result:
                raise ValueError("Conflicting immutable settlement")
            if old is None:
                s["settlements"] = (s["settlements"] + [result])[-8:]
            s["pending_settlements"] = [r for r in s["pending_settlements"] if r["hand_id"] != key]
        elif not any(r["hand_id"] == key for r in s["settlements"]):
            pending = {"hand_id": key, **d}
            previous = [
                r
                for r in s["pending_settlements"]
                if not (r["hand_id"] == key and r["received"] == d["received"])
            ]
            s["pending_settlements"] = (previous + [pending])[-16:]
    elif kind == "capture_gap":
        s.update(gap=True, history_complete=False, turn=None)
    else:
        if event.hand_id != s["hand_id"]:
            if (
                s["table_id"] == event.table_id
                and s["hand_id"] is not None
                and int(event.hand_id.split(":")[1]) < int(s["hand_id"].split(":")[1])
            ):
                raise ValueError("Older hand cannot replace current observation")
            old = s
            s = empty_state()
            s.update(
                table_id=event.table_id,
                hand_id=event.hand_id,
                settlements=old["settlements"],
                pending_settlements=old["pending_settlements"],
            )
        if kind == "snapshot":
            s.update(deepcopy(d))
            if s["gap"] or s["closed"]:
                s.update(history_complete=False, turn=None)
        elif kind == "hand_started":
            s["history_complete"] = d["history_complete"] and not s["gap"] and not s["closed"]
        elif kind == "hero_cards":
            s["hero_cards"] = d["cards"][:]
        elif kind == "board_updated":
            s["board"] = d["cards"][:]
        elif kind == "players_updated":
            s["players"] = deepcopy(d["players"])
        elif kind in ("action_confirmed", "action_updated"):
            old = next(
                (a for a in s["actions"] if a["source_event_id"] == d["source_event_id"]), None
            )
            if old:
                for key, value in d.items():
                    if old[key] is not None and value is not None and old[key] != value:
                        raise ValueError("Conflicting action identity")
                old.update({k: v for k, v in d.items() if v is not None})
            else:
                if kind == "action_updated":
                    s.update(gap=True, history_complete=False)
                if len(s["actions"]) >= 256:
                    raise ValueError("Hand action limit exceeded")
                if (
                    s["actions"]
                    and int(d["source_event_id"]) != int(s["actions"][-1]["source_event_id"]) + 1
                ):
                    s.update(gap=True, history_complete=False)
                s["actions"].append(deepcopy(d))
            if s["turn"] and s["turn"]["player_id"] == d["player_id"]:
                s["turn"] = None
        elif kind == "action_requested":
            s["turn"] = None if s["gap"] or s["closed"] else deepcopy(d["turn"])
        elif kind == "room_closed":
            s.update(closed=True, history_complete=False, turn=None)
    # Separate card deltas can temporarily overlap during a reset. The complete
    # snapshot is the atomic card consistency boundary, already contract-validated.
    if kind == "snapshot":
        unique_cards(s["hero_cards"] + s["board"])
    return s


def response(source, through, state, *, public_hand_verified=False, ledger_verified=False):
    # Legal betting replay proves only the roster and opening cash, not the policy or escrow.
    missing = [
        "verified_rule_profile",
        "supported_strategy_model",
        "hand_participants",
        "button_seat",
        "aligned_starting_stacks",
        "mushroom_ledger_anchor",
        "model_context_mapping",
    ]
    if public_hand_verified:
        missing.remove("hand_participants")
        missing.remove("aligned_starting_stacks")
        if ledger_verified:
            missing.remove("mushroom_ledger_anchor")
    if state["hand_id"] is None:
        missing.append("hand_id")
    if not state["history_complete"]:
        missing.append("complete_action_history")
    if None in state["hero_cards"]:
        missing.append("hero_cards")
    if not state["players"] or any(p["stack"] is None for p in state["players"]):
        missing.append("player_stacks")
    if state["turn"] is None:
        missing.append("current_turn")
    return {
        "schema": STATE_SCHEMA,
        "type": "capture.state",
        "source_id": source,
        "through_sequence": through,
        "projection_version": "wpk-observation/1",
        "state": state,
        "analysis": {
            "status": "unsupported",
            "reason": "unverified_rules_and_strategy_coverage",
            "actions": [],
            "missing_fields": missing,
        },
    }
