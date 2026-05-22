"""
schemas.py — Axiom Learning OS Session 6
Pydantic v2 data contracts for the entire agent framework.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, model_validator


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str = Field(min_length=1)
    value: dict[str, Any]
    artifact_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Goals / Observation
# ---------------------------------------------------------------------------

class Goal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)          # Positional identity — must never change across turns
    text: str = Field(min_length=1)
    done: bool
    attach_artifact_id: Optional[str] = None


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goals: list[Goal]
    all_done: bool


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class DecisionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: Optional[str] = None
    tool_call: Optional[ToolCall] = None

    @model_validator(mode="after")
    def validate_content(self) -> "DecisionOutput":
        if not self.answer and not self.tool_call:
            raise ValueError("Must provide either 'answer' or 'tool_call'")
        return self
