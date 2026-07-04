# fcoin — Synthetic Asset Trading MCP Agent

Universal MCP agent scaffold for trading a synthetic asset (fcoin) on a simulated
exchange. Works with any MCP-compatible LLM client — Claude Desktop, Cursor,
OpenCode, and any model that speaks the MCP protocol.

---

## Deploy to DigitalOcean (App Platform)

### One-click via GitHub Actions (recommended)

This repo includes a GitHub Actions workflow (`.github/deploy.yml`) that deploys to DigitalOcean App Platform on every push to `main`.

**Setup (one-time):**

1. Fork or push this repo to GitHub
2. Go to [cloud.digitalocean.com](https://cloud.digitalocean.com) → **Create → Apps**
3. Select **GitHub** → authorize DigitalOcean → pick your `fcoin` repo
4. Branch: **`main`**
5. App Platform auto-detects `app.yaml` at the repo root
6. Confirm the settings below, then click **Create Resource**

### App Settings (auto-populated from `app.yaml`)

| Setting | Value |
|---|---|
| **Build Command** | `pip install --no-cache-dir -e .` |
| **Run Command** | `python -m src` |
| **HTTP Port** | `8080` |
| **Health Check** | `GET /health` |

### Manual Deploy (no GitHub Actions)

```bash
# Clone the repo on a DigitalOcean Droplet
git clone https://github.com/viprocket1/fcoin.git
cd fcoin
docker build -t fcoin-agent .
docker run -d -p 8080:8080 --restart unless-stopped fcoin-agent
```

> **HTTPS is enabled automatically** on App Platform — no nginx or certbot needed.

### Connect MCP client

Once deployed, your app will be at:
```
https://fcoin-agent-<random-id>.ondigitalocean.app
```

Add to your MCP client config:
```json
"mcpServers": {
  "fcoin": {
    "url": "https://fcoin-agent-<your-app-name>.ondigitalocean.app/events",
    "transport": "sse"
  }
}
```

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
