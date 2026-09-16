# foundr-recruiting-agent

Slack agent: tag **`@Gold gold`** (any case) in a candidate thread in `#candidates` and it
replies with the top Paraform roles for that person, scored on skills, seniority, location
and domain. Compensation is ignored by design.

Architecture, tool list, workflow diagrams and risks: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Sample Slack card: [`slack/match_message.blocks.json`](slack/match_message.blocks.json).

> Paraform has no public API and `/browse` is behind login, so the catalogue comes from an
> authenticated Playwright session using a recruiter's own account. See the doc for the checks.

## Going live: the four things only an account owner can do

| # | What | Where | Produces |
|---|---|---|---|
| 1 | Create the Slack app **from the manifest** | https://api.slack.com/apps → Create New App → From a manifest → paste [`slack/app_manifest.yaml`](slack/app_manifest.yaml) | `SLACK_BOT_TOKEN` (Install App page) and `SLACK_APP_TOKEN` (Basic Information → App-Level Tokens, scope `connections:write`) |
| 2 | Create an API key | https://console.anthropic.com → API Keys | `ANTHROPIC_API_KEY` |
| 3 | Log in to Paraform once, on a machine with a screen | `python scripts/paraform_login.py` | `paraform_state.json` (never commit it) |
| 4 | Pick a host that runs a long-lived container | Fly.io (`fly.toml`) or Railway (`railway.toml`) | a running bot |

Then `/invite @Gold` in `#candidates`. The channel id is already in `.env.example`.

### Local run (fastest way to see it work)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env              # paste the three tokens from steps 1–2
python scripts/paraform_login.py  # step 3
python -m app.main
```

### Fly.io

```bash
fly launch --no-deploy --copy-config --name foundr-gold-agent
fly secrets set SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... ANTHROPIC_API_KEY=sk-ant-...
fly secrets set PARAFORM_STORAGE_STATE_B64="$(base64 -w0 paraform_state.json)"
fly deploy
```

### Railway

New project → Deploy from GitHub repo → this repo. Add the variables from `.env.example`
plus `PARAFORM_STORAGE_STATE_B64` (the base64 of `paraform_state.json`). Railway reads
`railway.toml` for the build and start command.

The session file is written to `PARAFORM_STORAGE_STATE` at startup from the base64 variable,
so no host needs to support secret files.

## Test

```bash
pytest -q
```
