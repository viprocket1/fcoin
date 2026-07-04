# fcoin — Synthetic Asset Trading MCP Agent

Universal MCP agent scaffold for trading a synthetic asset (fcoin) on a simulated
exchange. Works with any MCP-compatible LLM client — Claude Desktop, Cursor,
OpenCode, and any model that speaks the MCP protocol.

---

## Deploy to DigitalOcean (App Platform)

**One-click link (opens in new tab):**
<a href="https://cloud.digitalocean.com/apps/new?repo=https://github.com/viprocket1/fcoin/tree/master" target="_blank">Deploy to DigitalOcean</a>

---

### Step-by-step (for the above link to work, repo must have an `app.yaml` at root — see below)

1. Open the link above — it pre-fills the GitHub repo
2. Verify branch is **master**
3. Click **Next** — DO will scan the repo for `app.yaml`
4. If prompted for settings, fill in:

   | Setting | Value |
   |---|---|
   | **Build Command** | `pip install --no-cache-dir -e .` |
   | **Run Command** | `python -m src` |
   | **HTTP Port** | `8080` |

5. Under **Health Checks** → set path to `/health`
6. Click **Create Resource**

> **HTTPS is enabled automatically** — no certbot or nginx needed.

---

## Architecture

```
src/
├── agent.py              # Session, ToolDef, LLMProvider (model-agnostic core)
├── server.py             # MCPServer — stdio + SSE, protocol-compliant
├── exchange.py           # Exchange, PriceFeed — mock orderbook + GBM price
├── providers/            # Anthropic, OpenAI, Ollama (plug-and-play)
├── tools/
│   └── trading.py        # 12 MCP trading tools
└── transport/sse.py      # HTTP/SSE for remote clients
```

---

## MCP Tools (12 total)

| Tool | Description |
|---|---|
| `get_ticker` | Current fcoin/USDC mid-price |
| `get_orderbook` | L2 book (bids/asks) |
| `get_trades` | Recent trade history |
| `get_balance` | USDC or fcoin balance |
| `get_position` | fcoin qty, price, unrealised PnL |
| `get_open_orders` | All unfilled orders |
| `market_buy` / `market_sell` | Market orders |
| `limit_buy` / `limit_sell` | Limit orders |
| `cancel_order` | Cancel an open order |
| `set_price` | Admin: override simulation price |

---

## Quick Start (local)

```bash
# stdio — for Claude Desktop, Cursor, any MCP client
python -m src

# SSE — for remote agents over HTTP
python -m src --transport sse --port 8080 --initial-price 100.0
```

---

## Connecting an LLM Agent

### DigitalOcean App Platform (deployed)
```json
"mcpServers": {
  "fcoin": {
    "url": "https://fcoin-agent-<your-app-name>.ondigitalocean.app/events",
    "transport": "sse"
  }
}
```

### Local / stdio (Claude Desktop)
```json
"mcpServers": {
  "fcoin": {
    "command": "python",
    "args": ["-m", "src"]
  }
}
```

---

## Customising the Exchange

```python
from src import init_exchange, Session, TOOLS
from src.providers.ollama_ import OllamaProvider

ex = init_exchange(
    initial_usdc=50_000,
    initial_fcoin=0,
    initial_price=100.0,
    volatility=0.001,
)

session = Session(system_prompt="You are a fcoin trader.")
for tool in TOOLS:
    session.register_tool(tool)
session.llm_provider = OllamaProvider(model="llama3")
```

---

## Docker (manual droplet)

```bash
docker build -t fcoin-agent .
docker run -d -p 8080:8080 --restart unless-stopped fcoin-agent
```

---

## Securing `set_price`

Before production, remove or guard the admin tool:

```python
ADMIN_TOKEN = "your-secret-token"

def set_price(price: float, token: str = "") -> dict:
    if token != ADMIN_TOKEN:
        return {"error": "unauthorized"}
    _price_feed.set_price(price)
    return {"price": price}
```

---

## License

MIT
