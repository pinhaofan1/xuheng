from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Optional
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.ai.value_objects.generation_chunk import GenerationChunk


# Modified for Xuheng: sensible default; explicit user limits are respected.
DEFAULT_MAX_OUTPUT_TOKENS = 8192


class GenerationConfig:
    """生成配置"""
    def __init__(
        self,
        model: str = "",
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 1.0,
        response_format: Optional[Dict] = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.response_format = response_format
        self.__post_init__()

    def __post_init__(self):
        """验证配置参数"""
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError("Temperature must be between 0.0 and 2.0")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")
        self.max_tokens = int(self.max_tokens)


class GenerationResult:
    """生成结果"""
    def __init__(self, content: str, token_usage: TokenUsage):
        self.content = content
        self.token_usage = token_usage
        self.__post_init__()

    def __post_init__(self):
        """验证结果参数"""
        if not self.content or not self.content.strip():
            raise ValueError("Content cannot be empty")


class LLMService(ABC):
    """LLM 服务接口（领域服务）"""

    @abstractmethod
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成内容"""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容"""
        pass

    async def stream_generate_with_usage(self,prompt: Prompt,config: GenerationConfig) -> AsyncIterator[GenerationChunk]:
        """Compatible default for providers that only expose text deltas."""
        from domain.ai.services.generation_stream import closing_generation_stream
        stream=self.stream_generate(prompt,config)
        async with closing_generation_stream(stream):
            async for text in stream:
                yield GenerationChunk(text=text)
