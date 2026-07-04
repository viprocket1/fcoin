# DigitalOcean One-Click Deploy Guide

## Step 1 — Fork or push this repo to GitHub

Make sure your code is on GitHub (you already pushed to `viprocket1/fcoin`).

## Step 2 — Create the App on DigitalOcean

1. Log in to [cloud.digitalocean.com](https://cloud.digitalocean.com)
2. Click **Create → Apps**
3. Under **GitHub**, click **Manage Access** → Authorize DigitalOcean
4. Select your **`fcoin`** repository
5. Branch: **`main`**

## Step 3 — Configure the app

| Setting | Value |
|---|---|
| **Build Command** | `pip install --no-cache-dir -e .` |
| **Run Command** | `python -m src` |
| **HTTP Port** | `8080` |
| **Health Check Path** | `/health` |

The `.do/app.yaml` file is included in the repo and will be detected automatically.

## Step 4 — Add environment variable

Under **Environment Variables**, add:

| Key | Value | Type |
|---|---|---|
| `PORT` | `8080` | General |

## Step 5 — Deploy

Click **Create Resource**. Wait 2–3 minutes for the build.

Your app will be live at:
```
https://fcoin-agent-<random-id>.ondigitalocean.app
```

## Step 6 — Connect an MCP Client

Add to your MCP client config:
```json
{
  "mcpServers": {
    "fcoin": {
      "url": "https://fcoin-agent-<your-app-name>.ondigitalocean.app/events",
      "transport": "sse"
    }
  }
}
```

## Troubleshooting

- **Build fails**: check that the build command is exactly `pip install --no-cache-dir -e .`
- **Health check fails**: the app needs a `/health` HTTP endpoint — the agent already handles this
- **Can't connect**: make sure your MCP client uses `https://` (DO provides TLS automatically)
