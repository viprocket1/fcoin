"""OpenAI GPT LLM provider."""
import os
from typing import Any
from openai import AsyncOpenAI
from fcoin.src.agent import LLMProvider, ToolDef


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str = "gpt-4o"):
        self.client = AsyncOpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self.model = model

    async def complete(self, messages: list[dict], tools: list[ToolDef], **kwargs) -> str:
        tool_schema = [
            {"type": "function", "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }}
            for t in tools
        ]

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tool_schema,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        return msg.content or ""
