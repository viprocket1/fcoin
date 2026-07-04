"""
Core agent — completely transport/MCP agnostic.
Plug in any LLM provider (OpenAI, Anthropic, Ollama, etc.)
and any tool registry to get a session that executes turns.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# ---------------------------------------------------------------------------
# Tool / Resource types
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]  # sync or async


@dataclass
class ResourceDef:
    uri: str
    name: str
    description: str
    mime_type: str = "text/plain"
    handler: Callable[..., Any] | None = None  # optional async read handler


# ---------------------------------------------------------------------------
# LLM provider interface (swap in OpenAI, Anthropic, Ollama, etc.)
# ---------------------------------------------------------------------------

class LLMProvider(Protocol):
    """Implement this to add a new model provider."""

    async def complete(self, messages: list[dict], tools: list[ToolDef], **kwargs) -> str:
        """Send a chat completion request and return the assistant text."""
        ...


# ---------------------------------------------------------------------------
# Turn / Session
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    role: str          # "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class Session:
    """Holds conversation history and executes the agent loop."""
    system_prompt: str = ""
    turns: list[Turn] = field(default_factory=list)
    tool_registry: dict[str, ToolDef] = field(default_factory=dict)
    resource_registry: dict[str, ResourceDef] = field(default_factory=dict)
    max_turns: int = 20
    llm_provider: LLMProvider | None = None

    def add_turn(self, role: str, content: str,
                 tool_call_id: str | None = None, tool_name: str | None = None) -> None:
        self.turns.append(Turn(role, content, tool_call_id, tool_name))

    def register_tool(self, tool: ToolDef) -> None:
        self.tool_registry[tool.name] = tool

    def register_resource(self, res: ResourceDef) -> None:
        self.resource_registry[res.uri] = res

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> str:
        """Process one user message and return the final text response."""
        self.add_turn("user", user_message)

        limit = self.max_turns if self.max_turns > 0 else float("inf")
        turns = 0
        while turns < limit:
            turns += 1
            messages = self._build_messages()
            tools = list(self.tool_registry.values())

            if not self.llm_provider:
                raise RuntimeError("No LLM provider configured on this session.")

            text = await self.llm_provider.complete(messages, tools)

            self.add_turn("assistant", text)
            tool_calls = self._parse_tool_calls(text)

            if not tool_calls:
                return text  # Done — natural response

            for call in tool_calls:
                name, args, call_id = call["name"], call.get("arguments", {}), call["id"]
                tool = self.tool_registry.get(name)
                if not tool:
                    result = f"Error: unknown tool '{name}'"
                else:
                    try:
                        raw = tool.handler(**args)
                        result = raw if asyncio.iscoroutine(raw) else await asyncio.to_thread(lambda: raw)
                    except Exception as exc:
                        result = f"Error: {exc}"
                self.add_turn("tool", str(result), tool_call_id=call_id, tool_name=name)

        return "Error: max turns exceeded."

    # ------------------------------------------------------------------
    # Helpers — override or extend these to customise MCP protocol shape
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        for t in self.turns:
            if t.role == "tool":
                msgs.append({
                    "role": "tool",
                    "content": t.content,
                    "tool_call_id": t.tool_call_id,
                    "name": t.tool_name,
                })
            else:
                msgs.append({"role": t.role, "content": t.content})
        return msgs

    @staticmethod
    def _parse_tool_calls(text: str) -> list[dict]:
        """
        Override to customise how the LLM text is parsed into tool calls.
        Default handles a simple JSON block like:
          <tool_call>{"name": "...", "id": "...", "arguments": {...}}</tool_call>
        """
        import json, re
        pattern = re.compile(r'<tool_call>(.*?)</tool_call>', re.DOTALL)
        out = []
        for m in pattern.finditer(text):
            try:
                out.append(json.loads(m.group(1)))
            except Exception:
                pass
        return out
