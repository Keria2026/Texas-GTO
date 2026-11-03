"""Validated contracts for the local, synthetic-data research service."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GameConfig(StrictModel):
    game_id: Literal["kuhn", "leduc", "holdem", "mushroom"] = "kuhn"
    abstraction: Literal["none", "suit_canonical", "mushroom_context", "mushroom_population"] = (
        "none"
    )
    num_players: int = Field(default=2, ge=2, le=8)
    stack: int = Field(default=20, ge=2, le=1000)
    stacks: list[int] | None = Field(default=None, min_length=2, max_length=8)
    small_blind: int = Field(default=1, ge=1, le=100)
    big_blind: int = Field(default=2, ge=1, le=200)
    straddle: int | None = Field(default=None, ge=1)
    button: int = Field(default=0, ge=0, le=7)
    session_hands: int | None = Field(default=None, ge=1, le=4)

    @model_validator(mode="after")
    def compatible(self):
        if self.game_id == "mushroom":
            if self.num_players < 4 or (self.small_blind, self.big_blind, self.straddle) != (
                2,
                4,
                8,
            ):
                raise ValueError("Mushroom model requires 4–8 players and 2/4/8 forced bets")
            if self.abstraction not in ("none", "mushroom_context", "mushroom_population"):
                raise ValueError("Mushroom sessions require a mushroom abstraction")
            if self.abstraction == "mushroom_population" and (
                self.num_players != 8
                or self.stack != 800
                or self.stacks is not None
                or self.button != 0
            ):
                raise ValueError(
                    "Population uses eight model seats and its versioned opening distribution"
                )
            if self.stacks is not None and (
                len(self.stacks) != self.num_players or any(value < 0 for value in self.stacks)
            ):
                raise ValueError("Mushroom stacks must cover every seat with nonnegative integers")
            if self.button >= self.num_players:
                raise ValueError("button must be a participating seat")
            return self
        if self.session_hands is not None:
            raise ValueError("session_hands applies only to the mushroom training model")
        if self.abstraction in ("mushroom_context", "mushroom_population"):
            raise ValueError("Mushroom abstractions apply only to mushroom sessions")
        if self.abstraction != "none" and self.game_id != "holdem":
            raise ValueError("Suit canonical abstraction applies only to Holdem")
        if self.game_id == "leduc" and self.num_players != 2:
            raise ValueError("Leduc research rules require two players")
        if self.game_id == "kuhn" and self.num_players > 6:
            raise ValueError("Kuhn research rules support at most six players")
        if self.button >= self.num_players:
            raise ValueError("button must be a participating seat")
        if self.stacks is not None and (
            self.game_id != "holdem"
            or len(self.stacks) != self.num_players
            or any(value <= 0 for value in self.stacks)
        ):
            raise ValueError("stacks must provide one positive integer per Holdem seat")
        if self.small_blind > self.big_blind or (
            self.stacks is None and self.big_blind > self.stack
        ):
            raise ValueError("require small_blind <= big_blind and a funded uniform stack")
        if self.straddle is not None:
            if self.game_id != "holdem" or self.num_players < 4 or self.straddle <= self.big_blind:
                raise ValueError("live straddle requires 4–8 Holdem players and exceeds big_blind")
            seat = (self.button + 3) % self.num_players
            stack = self.stacks[seat] if self.stacks is not None else self.stack
            if stack < self.straddle:
                raise ValueError("short straddle is unsupported")
        return self


class CreateSession(StrictModel):
    game: GameConfig = Field(default_factory=GameConfig)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)


class SubmitAction(StrictModel):
    action: str = Field(min_length=1, max_length=80)
    event_id: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=0)


class TrainRequest(StrictModel):
    game: GameConfig = Field(default_factory=GameConfig)
    algorithm: Literal["cfr", "mccfr", "pluribus"] = "cfr"
    weighting: Literal["uniform", "linear"] = "linear"
    iterations: int = Field(default=1000, ge=1, le=100_000)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    max_nodes: int = Field(default=2_000_000, ge=1, le=2_000_000)
    deadline_seconds: float = Field(default=10.0, gt=0, le=20)
    pluribus: dict[str, Any] | None = None

    @model_validator(mode="after")
    def bounded(self):
        if self.game.game_id in ("holdem", "mushroom") and self.algorithm not in (
            "mccfr",
            "pluribus",
        ):
            raise ValueError("Holdem training supports bounded experimental MCCFR only")
        if self.algorithm == "pluribus":
            from texas_strategy.pluribus import PluribusConfig

            if self.weighting != "linear":
                raise ValueError("Pluribus requires scheduled linear discounting")
            if self.pluribus is not None:
                PluribusConfig.from_dict(self.pluribus)
        elif self.pluribus is not None:
            raise ValueError("Pluribus settings require algorithm=pluribus")
        return self


class AnalyzeRequest(StrictModel):
    session_id: str
    state_version: int = Field(ge=0)
    blueprint_id: str
    search: bool = False
    depth_limit: int = Field(default=1, ge=1, le=3)
    iterations: int = Field(default=100, ge=1, le=2000)
    max_nodes: int = Field(default=200_000, ge=1, le=300_000)
    deadline_seconds: float = Field(default=3.0, gt=0, le=10)

    @model_validator(mode="after")
    def identifiers(self):
        UUID(self.session_id)
        UUID(self.blueprint_id)
        return self


class EquityRequest(StrictModel):
    hero: list[str] = Field(min_length=2, max_length=2)
    board: list[str] = Field(default_factory=list, max_length=5)
    opponents: int = Field(default=1, ge=1, le=7)
    ranges: list[dict[str, float]] | None = Field(default=None, max_length=7)
    samples: int = Field(default=1000, ge=2, le=10_000)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)


class StrategyCapability(StrictModel):
    provider: Literal["pluribus_style"] = "pluribus_style"
    status: Literal["research_ready"] = "research_ready"
    supported_games: list[str] = Field(
        default_factory=lambda: ["kuhn", "leduc", "holdem", "mushroom"]
    )


class HealthResponse(StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    status: Literal["ok"] = "ok"
    service: Literal["texas-gto-analysis"] = "texas-gto-analysis"
    version: Literal["0.1.0"] = "0.1.0"
    strategy: StrategyCapability = Field(default_factory=StrategyCapability)


class CoreTrainingResponse(StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    enabled: bool
    phase: Literal[
        "disabled",
        "starting",
        "waiting_for_idle",
        "training",
        "paused",
        "saving",
        "limited",
        "error",
        "stopped",
    ] = "disabled"
    worker_pid: int | None = Field(default=None, ge=1)
    game: GameConfig
    algorithm: Literal["pluribus"] = "pluribus"
    objective: Literal["expected_net_points"] = "expected_net_points"
    iterations: int = Field(default=0, ge=0)
    information_sets: int = Field(default=0, ge=0)
    averaged_information_sets: int = Field(default=0, ge=0)
    checkpoint_bytes: int = Field(default=0, ge=0)
    quality: Literal["finite_horizon_unvalidated"] = "finite_horizon_unvalidated"
    recommendation_ready: Literal[False] = False
    reason: str | None = None


class SessionResponse(StrictModel):
    session_id: str
    state_version: int
    game: GameConfig
    rules_version: str
    actor: int | None
    terminal: bool
    legal_actions: list[str]
    observation: dict[str, Any]
    payoffs: list[float] | None


class JobResponse(StrictModel):
    job_id: str
    status: Literal["queued", "running", "completed", "cancelled", "failed"]
    diagnostics: dict[str, Any] | None = None
    blueprint_id: str | None = None
    evaluation: dict[str, Any] | None = None
    error: str | None = None


class ActionFrequency(StrictModel):
    action: str
    frequency: float = Field(ge=0, le=1)


class AnalysisResponse(StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    request_id: str
    session_id: str
    state_version: int
    status: Literal["ready", "unsupported"]
    strategy_source: Literal["blueprint", "depth_limited_search", "none"]
    rules_version: str
    model_version: str | None
    actions: list[ActionFrequency]
    warnings: list[str]
    diagnostics: dict[str, Any] | None = None


class HandStrengthRequest(StrictModel):
    hero: list[str] = Field(min_length=2, max_length=2)
    board: list[str] = Field(default_factory=list, max_length=5)
    opponent_range: dict[str, float] | None = None
    max_beating_combos: int = Field(default=5, ge=0, le=50)


class AdaptationRequest(StrictModel):
    blueprint_id: str
    hero: int = Field(default=0, ge=0, le=5)
    opponent_mode: Literal["call", "raise", "fold"] = "call"
    sample_count: int = Field(default=100, ge=0, le=1_000_000)
    max_worst_case_loss: float = Field(default=0.04, ge=0, le=1.0)

    @model_validator(mode="after")
    def identifier(self):
        UUID(self.blueprint_id)
        return self
