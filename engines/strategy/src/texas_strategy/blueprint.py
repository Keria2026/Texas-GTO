"""Versioned, checksummed JSON strategies and deterministic training checkpoints.

Checksums detect accidental corruption, not authenticity. Compatibility checks
bind artifacts to game/rule identifiers and player count. Action ordering is
always checked again at a policy query; ``validate_game`` additionally checks
all saved information sets against a bounded full-tree walk for small games.
No executable or pickle payload is accepted.
"""

import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

from texas_strategy.policy import PolicyEntry, TabularPolicy, legal_information

if TYPE_CHECKING:
    from texas_strategy.cfr import Trainer

SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 2
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Artifact must contain finite JSON values") from exc


def _checksum(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(document)).hexdigest()


def _write(path: str | Path, document: dict[str, Any]) -> None:
    content = _canonical({**document, "checksum": _checksum(document)}) + b"\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as f:
            temporary = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError(f"Nonfinite JSON value: {value}")


def _read(path: str | Path, *, kind: str, max_bytes: int) -> dict[str, Any]:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    with Path(path).open("rb") as f:
        content = f.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Artifact exceeds the size limit")
    try:
        document = json.loads(content, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid artifact JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Artifact root must be an object")
    digest = document.pop("checksum", None)
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("Artifact checksum missing or malformed")
    if not hmac.compare_digest(digest, _checksum(document)):
        raise ValueError("Artifact checksum mismatch")
    if type(document.get("schema_version")) is not int or document["schema_version"] not in (
        (SCHEMA_VERSION, CHECKPOINT_SCHEMA_VERSION) if kind == "checkpoint" else (SCHEMA_VERSION,)
    ):
        raise ValueError("Unsupported artifact schema version")
    if document.get("kind") != kind:
        raise ValueError("Wrong artifact kind")
    return document


def _fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"Invalid {label} fields")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid {label}")
    return value


def _metadata(metadata: Any, game: Any = None) -> dict[str, Any]:
    is_pluribus = isinstance(metadata, dict) and metadata.get("algorithm") == "pluribus"
    _fields(
        metadata,
        {
            "game_id",
            "rules_version",
            "num_players",
            "algorithm",
            "algorithm_version",
            "chance_sampling",
            "weighting",
            "iterations",
            "seed",
            "exploration",
        }
        | ({"pluribus"} if is_pluribus else set()),
        "metadata",
    )
    for field in ("game_id", "rules_version"):
        if not isinstance(metadata[field], str) or not metadata[field]:
            raise ValueError(f"Invalid {field}")
    _integer(metadata["num_players"], "num_players", minimum=1)
    _integer(metadata["iterations"], "iterations")
    if type(metadata["seed"]) is not int:
        raise ValueError("Invalid seed")
    if metadata["algorithm"] not in ("cfr", "mccfr", "pluribus"):
        raise ValueError("Unknown training algorithm")
    from texas_strategy.cfr import ALGORITHM_VERSION

    version = ALGORITHM_VERSION
    if is_pluribus:
        from texas_strategy.pluribus import PluribusConfig

        version = PluribusConfig.from_dict(metadata["pluribus"]).algorithm_version
        if metadata["weighting"] != "linear" or metadata["exploration"] != 0:
            raise ValueError("Invalid Pluribus weighting or exploration")
    if metadata["algorithm_version"] != version:
        raise ValueError("Unsupported training algorithm version")
    if metadata["chance_sampling"] not in ("enumerated", "game_exact"):
        raise ValueError("Unknown chance sampling method")
    if metadata["algorithm"] == "cfr" and metadata["chance_sampling"] != "enumerated":
        raise ValueError("Exact CFR must enumerate chance outcomes")
    if metadata["weighting"] not in ("uniform", "linear"):
        raise ValueError("Unknown iteration weighting")
    exploration = metadata["exploration"]
    if (
        type(exploration) not in (int, float)
        or not isfinite(exploration)
        or (exploration != 0 if is_pluribus else not 0 < exploration <= 1)
    ):
        raise ValueError("Invalid exploration probability")
    if game is not None:
        for field in ("game_id", "rules_version", "num_players"):
            if metadata[field] != getattr(game, field, None):
                raise ValueError(f"Artifact is incompatible with game {field}")
        validate = getattr(game, "validate_checkpoint_iterations", None)
        if validate is not None:
            validate(metadata["iterations"])
    return dict(metadata)


def _trainer_metadata(trainer: "Trainer") -> dict[str, Any]:
    from texas_strategy.cfr import ALGORITHM_VERSION

    if (
        getattr(trainer, "_uses_root_proposal", False)
        or getattr(trainer.game, "sample_training_root", None) is not None
    ):
        raise ValueError("Local training-root proposals cannot be persisted as core artifacts")
    extra = {}
    if trainer.algorithm == "pluribus":
        ALGORITHM_VERSION = trainer.pluribus.algorithm_version
        extra["pluribus"] = trainer.pluribus.to_dict()

    return _metadata(
        {
            "game_id": getattr(trainer.game, "game_id", None),
            "rules_version": getattr(trainer.game, "rules_version", None),
            "num_players": trainer.game.num_players,
            "algorithm": trainer.algorithm,
            "algorithm_version": ALGORITHM_VERSION,
            "chance_sampling": (
                "game_exact"
                if trainer.algorithm in ("mccfr", "pluribus")
                and callable(getattr(trainer.game, "sample_chance_outcome", None))
                else "enumerated"
            ),
            "weighting": trainer.weighting,
            "iterations": trainer.iterations,
            "seed": trainer.seed,
            "exploration": trainer.exploration,
            **extra,
        },
        trainer.game,
    )


def _entry_key(record: dict[str, Any], num_players: int) -> tuple[int, str]:
    player = _integer(record["player"], "entry player")
    information_key = record["information_key"]
    if player >= num_players or not isinstance(information_key, str) or not information_key:
        raise ValueError("Invalid entry information set")
    if not isinstance(record["actions"], list):
        raise ValueError("Entry actions must be an array")
    return player, information_key


class Blueprint:
    def __init__(self, metadata: dict[str, Any], policy: TabularPolicy) -> None:
        self.metadata = _metadata(metadata)
        if any(player >= self.metadata["num_players"] for player, _ in policy.entries):
            raise ValueError("Policy contains a player outside the game")
        if self.metadata["iterations"] == 0 and policy.entries:
            raise ValueError("Untrained blueprint cannot contain accumulated policies")
        self.policy = policy

    @classmethod
    def from_trainer(cls, trainer: "Trainer") -> "Blueprint":
        return cls(_trainer_metadata(trainer), trainer.average_policy())

    def _document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "blueprint",
            "metadata": _metadata(self.metadata),
            "entries": [
                {
                    "player": player,
                    "information_key": key,
                    "actions": list(entry.actions),
                    "probabilities": list(entry.probabilities),
                }
                for (player, key), entry in sorted(self.policy.entries.items())
            ],
        }

    @property
    def checksum(self) -> str:
        return _checksum(self._document())

    def save(self, path: str | Path) -> None:
        _write(path, self._document())

    @classmethod
    def load(
        cls, path: str | Path, *, game: Any = None, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> "Blueprint":
        document = _read(path, kind="blueprint", max_bytes=max_bytes)
        _fields(document, {"schema_version", "kind", "metadata", "entries"}, "blueprint")
        metadata = _metadata(document["metadata"], game)
        if not isinstance(document["entries"], list):
            raise ValueError("Blueprint entries must be an array")
        entries = {}
        for record in document["entries"]:
            _fields(
                record, {"player", "information_key", "actions", "probabilities"}, "policy entry"
            )
            key = _entry_key(record, metadata["num_players"])
            if key in entries:
                raise ValueError("Duplicate information set")
            if not isinstance(record["probabilities"], list):
                raise ValueError("Entry probabilities must be an array")
            entries[key] = PolicyEntry(tuple(record["actions"]), tuple(record["probabilities"]))
        return cls(metadata, TabularPolicy(entries))

    def validate_game(self, game: Any, *, max_nodes: int = 1_000_000) -> int:
        """Check all saved keys/actions against a complete small-game traversal.

        Raises if the validation budget is exhausted or any saved information
        set is absent. Large-game callers use the query-time action checks.
        Returns visited histories, not distinct states.
        """
        from texas_strategy.cfr import chance_distribution

        _metadata(self.metadata, game)
        _integer(max_nodes, "max_nodes", minimum=1)
        seen = set()
        pending = [game.initial_state()]
        nodes = 0
        while pending:
            if nodes >= max_nodes:
                raise ValueError("Game validation node budget exhausted")
            state = pending.pop()
            nodes += 1
            actor = game.actor(state)
            if actor is None:
                continue
            if actor == -1:
                actions = tuple(action for action, _ in chance_distribution(game, state))
            else:
                key, actions = legal_information(game, state)
                entry = self.policy.entries.get(key)
                if entry is not None:
                    if entry.actions != actions:
                        raise ValueError("Artifact actions do not match game information set")
                    seen.add(key)
            pending.extend(game.transition(state, action) for action in actions)
        if seen != set(self.policy.entries):
            raise ValueError("Artifact contains information sets absent from the game")
        return nodes


def save_checkpoint(trainer: "Trainer", path: str | Path) -> None:
    action_sets = sorted({entry.actions for entry in trainer._table.values()})
    action_index = {actions: i for i, actions in enumerate(action_sets)}
    document = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": "checkpoint",
        "metadata": _trainer_metadata(trainer),
        "rng_state": trainer._rng.getstate(),
        "training_context": getattr(trainer.game, "checkpoint_context", None),
        "action_sets": action_sets,
        "entries": [
            [
                player,
                key,
                action_index[entry.actions],
                entry.regrets,
                entry.strategy_sum,
                *(
                    [trainer._betting_rounds[(player, key)]]
                    if trainer.algorithm == "pluribus"
                    else []
                ),
            ]
            for (player, key), entry in sorted(trainer._table.items())
        ],
    }
    _write(path, document)


def _checkpoint_entries(document, *, pluribus):
    """Expand one compact row at a time; keep the strict v1 record validation."""
    entries = document["entries"]
    if not isinstance(entries, list):
        raise ValueError("Checkpoint entries must be an array")
    if document["schema_version"] == 1:
        yield from entries
        return
    action_sets = document["action_sets"]
    if not isinstance(action_sets, list):
        raise ValueError("Checkpoint action sets must be an array")
    unique = set()
    for actions in action_sets:
        if not isinstance(actions, list) or not actions:
            raise ValueError("Invalid checkpoint action set")
        PolicyEntry(tuple(actions), (1 / len(actions),) * len(actions))
        if tuple(actions) in unique:
            raise ValueError("Duplicate checkpoint action set")
        unique.add(tuple(actions))
    for row in entries:
        if not isinstance(row, list) or len(row) != (6 if pluribus else 5):
            raise ValueError("Invalid compact checkpoint row")
        index = _integer(row[2], "action set index")
        if index >= len(action_sets):
            raise ValueError("Invalid checkpoint action set index")
        yield {
            "player": row[0],
            "information_key": row[1],
            "actions": action_sets[index],
            "regrets": row[3],
            "strategy_sum": row[4],
            **({"betting_round": row[5]} if pluribus else {}),
        }


def load_checkpoint(
    game: Any, path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> "Trainer":
    from texas_strategy.cfr import Trainer, _TableEntry

    if getattr(game, "sample_training_root", None) is not None:
        raise ValueError("Core checkpoints cannot be restored into a local training-root proposal")
    document = _read(path, kind="checkpoint", max_bytes=max_bytes)
    _fields(
        document,
        {"schema_version", "kind", "metadata", "entries", "rng_state"}
        | (
            {"action_sets", "training_context"}
            if document["schema_version"] == CHECKPOINT_SCHEMA_VERSION
            else set()
        ),
        "checkpoint",
    )
    metadata = _metadata(document["metadata"], game)
    if document["schema_version"] == CHECKPOINT_SCHEMA_VERSION and document[
        "training_context"
    ] != getattr(game, "checkpoint_context", None):
        raise ValueError("Checkpoint training context does not match the starting game")
    trainer = Trainer(
        game,
        algorithm=metadata["algorithm"],
        weighting=metadata["weighting"],
        seed=metadata["seed"],
        exploration=metadata["exploration"],
        pluribus=metadata.get("pluribus"),
    )
    trainer.iterations = metadata["iterations"]
    if _trainer_metadata(trainer)["chance_sampling"] != metadata["chance_sampling"]:
        raise ValueError("Checkpoint chance sampling does not match this game implementation")
    for record in _checkpoint_entries(document, pluribus=trainer.algorithm == "pluribus"):
        _fields(
            record,
            {"player", "information_key", "actions", "regrets", "strategy_sum"}
            | ({"betting_round"} if trainer.algorithm == "pluribus" else set()),
            "checkpoint entry",
        )
        key = _entry_key(record, game.num_players)
        if key in trainer._table:
            raise ValueError("Duplicate information set")
        actions = tuple(record["actions"])
        # Reuse the same strict action checks as policies, without inventing frequencies.
        if not actions:
            raise ValueError("Checkpoint entry must contain legal actions")
        PolicyEntry(actions, (1 / len(actions),) * len(actions))
        for field in ("regrets", "strategy_sum"):
            values = record[field]
            if (
                not isinstance(values, list)
                or len(values) != len(actions)
                or any(type(v) not in (int, float) or not isfinite(v) for v in values)
            ):
                raise ValueError(f"Invalid checkpoint {field}")
        if any(value < 0 for value in record["strategy_sum"]):
            raise ValueError("Strategy sums must be nonnegative")
        trainer._table[key] = _TableEntry(
            actions, list(record["regrets"]), list(record["strategy_sum"])
        )
        if trainer.algorithm == "pluribus":
            stage = _integer(record["betting_round"], "betting_round")
            if stage > game.final_betting_round:
                raise ValueError("Checkpoint betting round exceeds game boundary")
            trainer._betting_rounds[key] = stage
    state = document["rng_state"]
    if (
        not isinstance(state, list)
        or len(state) != 3
        or type(state[0]) is not int
        or state[0] != 3
        or not isinstance(state[1], list)
        or len(state[1]) != 625
        or any(type(v) is not int or not 0 <= v < 2**32 for v in state[1][:-1])
        or type(state[1][-1]) is not int
        or not 0 <= state[1][-1] <= 624
        or (state[2] is not None and (type(state[2]) not in (int, float) or not isfinite(state[2])))
    ):
        raise ValueError("Invalid checkpoint RNG state")
    checked_rng = Random()
    checked_rng.setstate((state[0], tuple(state[1]), state[2]))
    trainer._rng = checked_rng
    if trainer.iterations == 0 and trainer._table:
        raise ValueError("Untrained checkpoint cannot contain accumulated information sets")
    return trainer
