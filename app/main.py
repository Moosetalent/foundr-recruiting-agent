"""Slack entrypoint. Socket Mode, so no public URL is needed.

Trigger: an app_mention whose text contains the word "gold" (any case), e.g.
    @claude Gold      @claude gold please
inside a thread in #candidates. The bot reads the whole thread, matches the
candidate against Paraform, and replies in the same thread.
"""
from __future__ import annotations

import logging
import time

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import blocks as bk
from .config import load_settings
from .matcher import extract_profile, match_candidate
from .paraform import ParaformAuthError, ParaformScrapeError, get_jobs
from .slack_io import collect_thread_context, is_gold_request, post_blocks, post_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent")

settings = load_settings()
app = App(token=settings.slack_bot_token)
claude = anthropic.Anthropic()

_recent: dict[str, float] = {}  # (channel, thread) -> last run; dedupes Slack retries


def _dedupe(key: str, window_s: int = 30) -> bool:
    now = time.time()
    if now - _recent.get(key, 0) < window_s:
        return True
    _recent[key] = now
    return False


@app.event("app_mention")
def handle_mention(event, client):
    text = event.get("text", "")
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]

    if settings.candidates_channel_id and channel != settings.candidates_channel_id:
        return
    if not is_gold_request(text):
        return
    if _dedupe(f"{channel}:{thread_ts}"):
        return

    client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
    try:
        content = collect_thread_context(client, settings.slack_bot_token, channel, thread_ts)
        if not content:
            post_error(client, channel, thread_ts, "I couldn't find a candidate profile in this thread. Attach a resume or paste the profile, then tag me again.")
            return

        profile = extract_profile(claude, settings, content)
        jobs = get_jobs(settings, claude)
        report = match_candidate(claude, settings, profile, jobs)

        post_blocks(client, channel, thread_ts,
                    bk.build_match_blocks(profile, report, settings.paraform_browse_url),
                    bk.fallback_text(report))
        client.reactions_add(channel=channel, timestamp=event["ts"], name="white_check_mark")

    except ParaformAuthError as exc:
        log.error("paraform auth: %s", exc)
        post_error(client, channel, thread_ts, "Paraform session expired. An admin needs to run `python scripts/paraform_login.py` and restart me.")
    except ParaformScrapeError as exc:
        log.error("paraform scrape: %s", exc)
        post_error(client, channel, thread_ts, "I reached Paraform but couldn't read the job board. The page layout may have changed.")
    except anthropic.RateLimitError:
        post_error(client, channel, thread_ts, "Claude is rate-limited right now. Try again in a minute.")
    except anthropic.APIStatusError as exc:
        log.exception("anthropic error")
        post_error(client, channel, thread_ts, f"Claude API error ({exc.status_code}). Try again shortly.")
    except Exception:
        log.exception("unhandled error in gold handler")
        post_error(client, channel, thread_ts, "Something went wrong while matching. Check the agent logs.")


@app.action(bk.SUBMIT_ACTION_ID)
def handle_submit(ack, body, logger):
    """URL buttons open the Paraform job page client-side; we only acknowledge and log.
    Wire an actual submission here if Paraform ever exposes a write endpoint."""
    ack()
    user = body.get("user", {}).get("username") or body.get("user", {}).get("id")
    job_id = (body.get("actions") or [{}])[0].get("value")
    logger.info("submit clicked by %s for job %s", user, job_id)


if __name__ == "__main__":
    SocketModeHandler(app, settings.slack_app_token).start()
