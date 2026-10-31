from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrategyCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["pluribus_style"] = "pluribus_style"
    status: Literal["not_implemented"] = "not_implemented"
    supported_games: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["ok"] = "ok"
    service: Literal["texas-gto-analysis"] = "texas-gto-analysis"
    version: Literal["0.1.0"] = "0.1.0"
    strategy: StrategyCapability = Field(default_factory=StrategyCapability)
