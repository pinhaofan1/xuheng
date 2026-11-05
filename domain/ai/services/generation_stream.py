"""Adapt older string-only services and close async generators on early exit."""
from contextlib import asynccontextmanager

from domain.ai.value_objects.generation_chunk import GenerationChunk


@asynccontextmanager
async def closing_generation_stream(stream):
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


async def generation_events(service, prompt, config):
    # Looking at the class avoids invoking synthetic attributes on duck-typed
    # legacy clients and test doubles. New providers keep the string API too.
    supports_usage = callable(getattr(type(service), "stream_generate_with_usage", None))
    stream = (service.stream_generate_with_usage(prompt, config) if supports_usage
              else service.stream_generate(prompt, config))
    async with closing_generation_stream(stream):
        async for chunk in stream:
            if isinstance(chunk, GenerationChunk):
                yield chunk
            elif isinstance(chunk, str):
                yield GenerationChunk(text=chunk)
            else:
                raise TypeError("Unexpected generation stream item")
