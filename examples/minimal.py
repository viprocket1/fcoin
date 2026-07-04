"""Minimal MCP agent — runs a single tool over stdio."""
import sys
sys.path.insert(0, "..")
from src.agent import Session, ToolDef
from src.server import MCPServer


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def main():
    session = Session(
        system_prompt="You are a helpful calculator. Use the add tool when needed."
    )
    session.register_tool(ToolDef(
        name="add",
        description="Add two integers",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        handler=add,
    ))

    server = MCPServer(session=session)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
