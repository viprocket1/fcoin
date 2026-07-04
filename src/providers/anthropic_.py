"""Anthropic Claude LLM provider."""
import os
from typing import Any
from anthropic import AsyncAnthropic
from fcoin.src.agent import LLMProvider, ToolDef


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"):
        self.client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.model = model

    async def complete(self, messages: list[dict], tools: list[ToolDef], **kwargs) -> str:
        tool_schema = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            messages=messages,
            tools=tool_schema,
        )

        # Return combined text + any tool-use blocks
        parts = [block.text for block in response.content if hasattr(block, "text")]
        return "\n".join(parts) if parts else str(response.content)
