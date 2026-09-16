# foundr-recruiting-agent

Slack agent: tag **`@claude Gold`** (any case) in a candidate thread in `#candidates` and it
replies with the top Paraform roles for that person, scored on skills, seniority, location
and domain. Compensation is ignored by design.

Architecture, tool list, workflow diagrams and risks: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Sample Slack card: [`slack/match_message.blocks.json`](slack/match_message.blocks.json).

> Paraform has no public API and `/browse` is behind login, so the catalogue comes from an
> authenticated Playwright session using a recruiter's own account. See the doc for the checks.

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env            # fill Slack + Anthropic tokens
python scripts/paraform_login.py  # one-time, needs a display; saves paraform_state.json
python -m app.main
```

## Test

```bash
pytest -q
```
