# fcoin — Synthetic Asset Trading MCP Agent

Universal MCP agent scaffold for trading a synthetic asset (fcoin) on a simulated
exchange. Works with any MCP-compatible LLM client — Claude Desktop, Cursor,
OpenCode, and any model that speaks the MCP protocol.

---

## Deploy to Render (one-click)

<a href="https://render.com/deploy?repo=https://github.com/viprocket1/fcoin" target="_blank"><img src="https://render.com/images/deploy-to-render-button" width="150" alt="Deploy to Render"/></a>

Or manually: go to **https://render.com/deploy?repo=https://github.com/viprocket1/fcoin**

Render auto-detects `render.yaml` and fills in all settings. You just need to:
1. Click **Connect + Deploy**
2. Wait ~2 minutes for the build
3. Your app will be live at `https://fcoin-agent.onrender.com`

> **Free tier sleeps after 15 minutes of inactivity** — first wakeup takes ~30s.
> **HTTPS is enabled automatically.**

---

## One Link to Start Trading

Paste this into any AI prompt:

```
GET https://fcoin-agent.onrender.com/prompt?agent_id=my-agent
```

The AI reads the instructions and starts trading autonomously. Each `agent_id` is a separate wallet with 10,000 USDC.

---

No MCP client needed — paste into any AI prompt as plain HTTP calls.

**Base URL:** `https://fcoin-agent.onrender.com`

Each agent is identified by `X-Agent-ID` header. If omitted, defaults to `"default"`.
First trade for a new agent auto-creates a wallet with 10,000 USDC.

### Trade

```
POST /trade
X-Agent-ID: my-agent-1
Content-Type: application/json

{"action": "buy", "amount": 100}              # market buy $100 of fcoin
{"action": "sell", "amount": 50}              # market sell 50 fcoin
{"action": "buy", "amount": 100, "price": 105.5}  # limit buy at $105.50
{"action": "sell", "amount": 25, "price": 110}    # limit sell at $110
```

### Portfolio

```
GET /portfolio
X-Agent-ID: my-agent-1
```

### Ticker (shared market price)

```
GET /ticker
```

### Example AI prompts:

```
# Agent 1 gets their Ethereum wallet address
GET https://fcoin-agent.onrender.com/wallet
X-Agent-ID: agent-alpha

# Agent 1 buys $100 fcoin
POST https://fcoin-agent.onrender.com/trade
X-Agent-ID: agent-alpha
{"action": "buy", "amount": 100}

# Agent 2 checks their portfolio
GET https://fcoin-agent.onrender.com/portfolio
X-Agent-ID: agent-beta
```

---

## Deploy to DigitalOcean (App Platform)

### Step 1 — Create the App

1. Go to **https://cloud.digitalocean.com/apps/new**
2. Click **GitHub** under **Launch from GitHub**
3. Authorize DigitalOcean access to your GitHub account
4. Under **Repository**, select **`viprocket1/fcoin`**
5. Branch: **`master`**
6. Click **Next**

### Step 2 — Configure

On the **Configure App** screen, fill in:

| Setting | Value |
|---|---|
| **Build Command** | `pip install --no-cache-dir -e .` |
| **Run Command** | `python -m src --transport sse --port 8080` |
| **HTTP Port** | `8080` |

Scroll down to **Health Checks** and set:

| Setting | Value |
|---|---|
| **Protocol** | `HTTP` |
| **Path** | `/health` |

Click **Next**.

### Step 3 — Multi-Step Deploy (optional — skip if not shown)

No multi-step needed — click **Create Resource** (or **Create & Restart**).

### Step 4 — Done

Your app deploys. Once live, the URL will be:
```
https://fcoin-agent-<random-id>.ondigitalocean.app
```

> **HTTPS is enabled automatically** — no nginx or certbot needed.

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
