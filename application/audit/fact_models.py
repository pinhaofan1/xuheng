"""Contracts for evidence-backed author decisions."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)
    character_name: str = Field(min_length=1, max_length=40)
    assertion: str = Field(min_length=1, max_length=300)
    quote: str = Field(min_length=4, max_length=500)


class FactDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=80, pattern=r'^[\w-]+$')
    expected_version: int = Field(ge=0)
    rules_revision: str = Field(max_length=80)
    action: Literal['accept', 'coexist', 'reject', 'undo']
    profile_id: str | None = Field(default=None, max_length=80)
    replaces: str | None = Field(default=None, min_length=1, max_length=300)
    applies_from: int | None = Field(default=None, ge=1, le=100_000)
    applies_through: int | None = Field(default=None, ge=1, le=100_000)
