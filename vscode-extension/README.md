# AI Meter for VS Code

See how much of your AI plans you have left without leaving the editor.

![AI Meter](https://raw.githubusercontent.com/eslamelhefny/AImon/main/logo.png)

- **Status bar:** the limit closest to running out and when it comes back, for example `Claude 92% · 3d 4h`. It turns yellow at 80% and red at 95%.
- **Side panel:** every plan limit with a live countdown to its reset, plus tokens and cost for today and this month.
- **Notifications:** one heads-up when a plan limit passes 90% (you can change the threshold).

Works with everything AI Meter reads: Claude Pro/Max, ChatGPT/Codex, Kimi Code, OpenCode Go, OpenRouter, DeepSeek, Anthropic and OpenAI APIs, Nous Portal and Hermes Agent.

## Setup

The extension reads from AI Meter, which keeps your keys and talks to the providers. Your keys never go into VS Code.

1. Install and start AI Meter:
   ```bash
   pipx install ai-meter
   aimeter
   ```
   Connect your providers at http://localhost:8787.
2. That's it. The extension looks at `http://localhost:8787` by default.

**AI Meter on a server?** Open a tunnel and the defaults keep working:
```bash
ssh -N -L 8787:127.0.0.1:8787 you@your-server
```
If you set `AIMETER_DASHBOARD_TOKEN` on the server, run **AI Meter: Set dashboard token** once. VS Code keeps it in its secret storage.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `aimeter.serverUrl` | `http://localhost:8787` | Where AI Meter is running |
| `aimeter.refreshSeconds` | `60` | How often to read new numbers |
| `aimeter.statusBar` | `tightest` | `off` hides the status bar item |
| `aimeter.notifyAt` | `90` | Percent at which a plan limit notifies you |
| `aimeter.includeRateLimits` | `false` | Also list per-minute API rate limits |

## Commands

AI Meter: Refresh · Check every provider now · Open dashboard in browser · Set dashboard token · Clear dashboard token · Set server address

## License

MIT
