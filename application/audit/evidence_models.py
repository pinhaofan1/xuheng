"""Validated contracts for the Xuheng evidence review extension."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.novel.services.action_attribution import ACTION_LABELS, MAGIC_TYPES

ENGINE_VERSION = "xuheng-rules-1"
AI_ENGINE_VERSION = "xuheng-ai-1"


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class CharacterRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    name: str = Field(min_length=1, max_length=40)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    allowed_magic: list[str] = Field(default_factory=list, max_length=5)
    forbidden_actions: list[str] = Field(default_factory=list, max_length=8)
    facts: list[str] = Field(default_factory=list, max_length=24)

    @field_validator('facts')
    @classmethod
    def clean_facts(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value.strip()) > 300 or '\x00' in value for value in values):
            raise ValueError('每条人物事实需要 1—300 字，不能包含空字符')
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(c in value for c in "，,。！？!?；;\n\r\t“”\"'：:"):
            raise ValueError("人物名不能包含分句符号或引号")
        return value

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        if any(not x.strip() or len(x.strip()) > 40 for x in values):
            raise ValueError("别名需要为 1—40 字")
        return sorted({cls.clean_name(x) for x in values})

    @field_validator("allowed_magic")
    @classmethod
    def check_magic(cls, values: list[str]) -> list[str]:
        if not set(values).issubset(MAGIC_TYPES):
            raise ValueError("不支持的魔法类型")
        return sorted(set(values))

    @field_validator("forbidden_actions")
    @classmethod
    def check_actions(cls, values: list[str]) -> list[str]:
        if not set(values).issubset(ACTION_LABELS):
            raise ValueError("不支持的行为类型")
        return sorted(set(values))

    def source_text(self) -> str:
        parts = []
        if self.allowed_magic:
            parts.append(f"{self.name}仅能使用{'、'.join(self.allowed_magic)}")
        if self.forbidden_actions:
            parts.append(f"{self.name}明确禁止{'、'.join(ACTION_LABELS[x] for x in self.forbidden_actions)}")
        if self.facts:
            parts.append(f'{self.name}的人物事实：' + '；'.join(self.facts))
        return "；".join(parts) or f"{self.name}尚未设置行为限制"


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(default="", max_length=80)
    profiles: list[CharacterRule] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unambiguous_names(self):
        ids: set[str] = set()
        names: dict[str, str] = {}
        for profile in self.profiles:
            if profile.id in ids:
                raise ValueError("人物 ID 不能重复")
            ids.add(profile.id)
            for name in [profile.name, *profile.aliases]:
                if name in names and names[name] != profile.id:
                    raise ValueError(f"人物名或别名重复：{name}")
                names[name] = profile.id
        if sum(len(profile.source_text()) for profile in self.profiles) > 20_000:
            raise ValueError('人物设定合计最多 20000 字符，请精简后再保存')
        return self

    def content_hash(self) -> str:
        # Empty new fields must not expire existing runs and human feedback.
        profiles = []
        for profile in self.profiles:
            payload = profile.model_dump()
            if not payload['facts']:
                payload.pop('facts')
            profiles.append(payload)
        profiles.sort(key=lambda p: p['id'])
        return text_hash(json.dumps(profiles, ensure_ascii=False, sort_keys=True))


class RunReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_content: str | None = Field(default=None, max_length=100_000)


@dataclass(frozen=True)
class ReviewContext:
    rules: RuleSet
    facts: tuple[dict, ...] = ()

    def content_hash(self) -> str:
        rules_hash = self.rules.content_hash()
        if not self.facts:
            return rules_hash
        return text_hash(json.dumps([rules_hash, self.facts], ensure_ascii=False, sort_keys=True))


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "confirmed", "ignored"]
    expected_version: int = Field(ge=0)


class HistorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=8000)
    semantic: bool = False
    limit: int = Field(default=6, ge=1, le=10)
    retrieval_mode: Literal['bm25', 'hybrid', 'hybrid_rerank'] = 'bm25'


class AIReviewRequest(RunReviewRequest):
    semantic: bool = False
    output_tokens: int = Field(default=4096, ge=1024, le=8192)
    retrieval_mode: Literal['bm25', 'hybrid', 'hybrid_rerank'] = 'bm25'
