"""A text delta or final usage snapshot; usage snapshots are never token deltas."""
from dataclasses import dataclass

from domain.ai.value_objects.token_usage import TokenUsage


@dataclass(frozen=True)
class GenerationChunk:
    text: str = ""
    token_usage: TokenUsage | None = None

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise TypeError("Stream text must be a string")
        if self.token_usage is not None:
            if not isinstance(self.token_usage, TokenUsage) or any(
                type(value) is not int or value < 0
                for value in (self.token_usage.input_tokens, self.token_usage.output_tokens)
            ):
                raise ValueError("Stream usage must contain non-negative integer counts")
