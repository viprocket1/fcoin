"""Ollama (local) LLM provider — no API key needed."""
import httpx
from fcoin.src.agent import LLMProvider, ToolDef


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self.base_url = base_url
        self.model = model

    async def complete(self, messages: list[dict], tools: list[ToolDef], **kwargs) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]
