"""kittenclaw - the harness.

End-to-end runtime in one file. The control flow you want to understand is
`turn_loop` near the bottom; everything above it is supporting machinery
(config loading, system-prompt rendering, JSONL I/O, usage logging).

Cache notes for the curious student
-----------------------------------
The whole point of this file's shape is to keep the prompt **prefix** byte-
identical across turns of a conversation, so any provider doing prefix
caching gets a hit on every call after the first:

* The system prompt is the whole behaviour of this triage bot - one static
  `SYSTEM.md` file. It is read ONCE, when the conversation file is created,
  and written as the first JSONL line. Subsequent turns read it from disk
  verbatim - they never re-render. Editing `SYSTEM.md` affects *new*
  conversations only.
* Tool results are deterministic in shape (they are whatever the tool
  returned). They append to the message list, never mutate prior lines.

This file keeps the prefix cache-friendly but does not *measure* caching -
we log only the provider's raw prompt/completion/total token counts from
`response.usage`. Reading cache-hit telemetry is taught separately.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

import jsonlines
from dotenv import load_dotenv
from openai import AsyncOpenAI

from . import tools

# ---------------------------------------------------------------------------
# Hardcoded repo-root paths. See SPEC.md → "Hardcoded paths".
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "kittenclaw.toml"
SYSTEM_PROMPT_PATH = ROOT / "SYSTEM.md"
CONVERSATIONS_DIR = ROOT / "conversations"
ARCHIVE_DIR = CONVERSATIONS_DIR / "archive"
STICKER_PATH = ROOT / "kittenclaw.webp"

log = logging.getLogger("kittenclaw")


# ---------------------------------------------------------------------------
# Config loading: TOML + `${VAR}` env interpolation
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Walk the parsed TOML and substitute `${VAR}` references with their
    environment value. Unset vars raise - fail fast, no silent fallbacks."""
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            v = os.environ.get(name)
            if v is None:
                raise RuntimeError(
                    f"kittenclaw.toml references ${{{name}}} but it's not set in .env"
                )
            return v

        return _VAR_RE.sub(sub, value)
    return value


def load_config(preset_name: str | None = None) -> dict:
    """Load `kittenclaw.toml`, interpolate `${VAR}` references, return the
    selected preset dict. `preset_name=None` means use `default_preset`."""
    raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = _interpolate(raw)
    name = preset_name or cfg["default_preset"]
    models = cfg.get("models", {})
    if name not in models:
        raise SystemExit(
            f"preset {name!r} not found in kittenclaw.toml - available: {list(models)}"
        )
    preset = dict(models[name])
    preset["_name"] = name
    return preset


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def render_system_prompt() -> str:
    """Return the system prompt verbatim from `SYSTEM.md`.

    There is no templating: this is a single-purpose triage bot, so its whole
    behaviour lives in one static file. The prompt is read once, when a
    conversation is created (see `new_conversation`), and stored as the first
    JSONL line - so it is byte-identical on every later turn and the provider's
    prefix cache stays warm. Editing `SYSTEM.md` only affects *new*
    conversations; existing ones keep the prompt they were created with."""
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Conversation files (JSONL)
# ---------------------------------------------------------------------------

# Matches `<chat_id>-<serial>.jsonl`, capturing both fields.
_CONV_RE = re.compile(r"^(-?\d+)-(\d{3})\.jsonl$")


def _scan_serials(chat_id: int) -> list[int]:
    """All serial numbers seen for `chat_id`, across active + archive."""
    serials = []
    for d in (CONVERSATIONS_DIR, ARCHIVE_DIR):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            m = _CONV_RE.match(p.name)
            if m and int(m.group(1)) == chat_id:
                serials.append(int(m.group(2)))
    return serials


def active_conversation_path(chat_id: int) -> Path | None:
    """Return the active (top-level, non-archived) conversation file for
    `chat_id`, or None if there isn't one."""
    if not CONVERSATIONS_DIR.is_dir():
        return None
    for p in CONVERSATIONS_DIR.iterdir():
        m = _CONV_RE.match(p.name)
        if m and int(m.group(1)) == chat_id:
            return p
    return None


def has_ever_greeted(chat_id: int) -> bool:
    """True iff we have any record (active or archived) of this chat - used
    by the Telegram bot to decide whether to send the first-contact
    disclaimer. The filesystem *is* the greeted-users state."""
    return bool(_scan_serials(chat_id))


def new_conversation(chat_id: int) -> Path:
    """Create a fresh conversation file for `chat_id` with serial =
    max(existing) + 1 (or 001), seeded with the rendered system message."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    serial = (max(_scan_serials(chat_id), default=0)) + 1
    path = CONVERSATIONS_DIR / f"{chat_id}-{serial:03d}.jsonl"
    system_msg = {"role": "system", "content": render_system_prompt()}
    with jsonlines.open(path, mode="w") as w:
        w.write(system_msg)
    log.info("[chat %s] new conversation: %s", chat_id, path.name)
    return path


def read_messages(path: Path) -> list[dict]:
    """Load the full message list from a JSONL conversation file.
    `skip_invalid=True` tolerates a partial trailing line from a crash."""
    with jsonlines.open(path) as r:
        return list(r.iter(skip_invalid=True))


def append_message(path: Path, msg: dict) -> None:
    """Append a single message to the conversation file. One JSON object,
    terminated by `\\n` - the unit of atomicity."""
    with jsonlines.open(path, mode="a") as w:
        w.write(msg)


def archive(path: Path) -> None:
    """Move an active conversation file into `conversations/archive/`,
    keeping its filename (and therefore its serial) intact."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(ARCHIVE_DIR / path.name))
    log.info("archived %s", path.name)


# ---------------------------------------------------------------------------
# Model client + usage logging
# ---------------------------------------------------------------------------


def make_client(preset: dict) -> AsyncOpenAI:
    """OpenAI-compatible async client. Works against any provider that
    speaks /v1/chat/completions."""
    return AsyncOpenAI(base_url=preset["base_url"], api_key=preset["api_key"])


def _log_usage(chat_id: int, turn: int, usage: Any) -> int:
    """Print the one-line token summary; return prompt_tokens (the caller uses
    it for the auto-clear budget check). Reads `response.usage` directly - no
    tokenizer dependency."""
    if usage is None:
        log.info("[chat %s] turn %d  (no usage block returned)", chat_id, turn)
        return 0
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    tt = getattr(usage, "total_tokens", 0) or 0
    log.info(
        "[chat %s] turn %d  prompt=%d  completion=%d  total=%d",
        chat_id,
        turn,
        pt,
        ct,
        tt,
    )
    return pt


async def call_model(
    client: AsyncOpenAI,
    preset: dict,
    messages: list[dict],
) -> Any:
    """One model call against the OpenAI-compatible chat-completions API."""
    return await client.chat.completions.create(
        model=preset["model"],
        messages=messages,
        tools=tools.TOOL_SCHEMAS,
        max_tokens=preset["max_response_tokens"],
        # REVIEW: assuming the SDK ≥1.40 path (max_tokens auto-routed to
        # max_completion_tokens for o-series). If a student hits a rejection
        # on a strict proxy, rename to `max_completion_tokens=` here.
    )


# ---------------------------------------------------------------------------
# The turn loop - the heart of the harness
# ---------------------------------------------------------------------------


async def turn_loop(
    client: AsyncOpenAI,
    preset: dict,
    chat_id: int,
    path: Path,
    user_text: str,
) -> tuple[str, bool]:
    """Run one Telegram-message → final-assistant-reply cycle.

    Reads the conversation file, appends the user message, calls the model
    in a loop (executing tool calls as they come back) until the model
    returns a final text reply, then writes everything in order.

    Returns `(reply_text, ended)`. `ended=True` means the conversation file has
    been archived and the caller should tell the user it is over - either because
    the triage bot called a disposition tool (`escalate` / `schedule_appointment`
    / `no_further_action`), or because the response pushed us past
    `max_context_tokens`. Both archive the file, so the next user message starts a
    fresh conversation; the caller does not need to know which reason fired.
    """
    messages = read_messages(path)

    user_msg = {"role": "user", "content": user_text}
    messages.append(user_msg)
    append_message(path, user_msg)

    turn = 0
    auto_clear_threshold = preset["max_context_tokens"]
    max_response = preset["max_response_tokens"]
    disposition_called = False

    while True:
        turn += 1
        resp = await call_model(client, preset, messages)
        prompt_tokens = _log_usage(chat_id, turn, getattr(resp, "usage", None))
        choice = resp.choices[0]
        m = choice.message

        # Persist the assistant message in the *exact* wire shape we'll send
        # back next time. `model_dump()` gives us that for free.
        # REVIEW: `exclude_none=True` strips e.g. `tool_calls: None`, which
        # is what the API expects to omit anyway. Keeps JSONL tidy.
        assistant_msg = m.model_dump(exclude_none=True)
        messages.append(assistant_msg)
        append_message(path, assistant_msg)

        # If the model called tools, run each and continue the loop.
        if m.tool_calls:
            for tc in m.tool_calls:
                content = await tools.dispatch(
                    tc.function.name, tc.function.arguments or "{}"
                )
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                }
                messages.append(tool_msg)
                append_message(path, tool_msg)
                # A disposition tool closes the call. We remember it and let the
                # model emit its closing line on the *next* turn (e.g. "I've
                # booked you for ..."), then archive below - so that closing
                # message is part of the saved transcript, not lost.
                if tc.function.name in tools.DISPOSITION_TOOLS:
                    disposition_called = True
                log.debug(
                    "[chat %s] turn %d tool %s -> %d chars",
                    chat_id,
                    turn,
                    tc.function.name,
                    len(content),
                )
            continue

        # No tool calls → this is the final reply.
        reply = (m.content or "").strip()

        # A disposition closed the triage call: archive (same mechanism as
        # /clear) so the next patient message starts a fresh conversation.
        if disposition_called:
            log.info("[chat %s] triage call closed by disposition", chat_id)
            archive(path)
            return reply, True

        # Auto-clear check: would the *next* turn fit a full response?
        if prompt_tokens + max_response >= auto_clear_threshold:
            log.warning(
                "[chat %s] auto-clear: prompt=%d + max_response=%d >= max_context=%d.",
                chat_id,
                prompt_tokens,
                max_response,
                auto_clear_threshold,
            )
            archive(path)
            return reply, True

        return reply, False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="kittenclaw")
    parser.add_argument("--preset", help="model preset name from kittenclaw.toml")
    parser.add_argument(
        "--verbose", action="store_true", help="per-tool-call debug logging"
    )
    parser.add_argument(
        "--once",
        metavar="MESSAGE",
        help="process one message locally and exit, no Telegram (for debugging)",
    )
    parser.add_argument(
        "--chat",
        type=int,
        default=0,
        help="conversation id for --once (default 0). Use different values to keep "
        "separate threads; reuse one to continue it.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx logs one INFO line per request ("HTTP Request: POST ... 200 OK").
    # Every model call goes through it (the openai SDK uses httpx underneath),
    # which drowns out our one-line-per-turn token summary. Mute it to WARNING;
    # --verbose can't bring it back, which is the point.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    preset = load_config(args.preset)
    log.info(
        "preset=%s model=%s base_url=%s",
        preset["_name"],
        preset["model"],
        preset["base_url"],
    )

    # --once drives a single turn through the same turn_loop the bot uses,
    # then exits - no Telegram token, no polling. Repeated calls reuse one
    # synthetic chat_id so the conversation file continues across them; the
    # rendered reply goes to stdout while logging stays on stderr.
    if args.once is not None:
        client = make_client(preset)
        chat_id = args.chat  # default 0; --chat N gives an independent thread
        path = active_conversation_path(chat_id) or new_conversation(chat_id)
        reply, ended = asyncio.run(
            turn_loop(
                client=client,
                preset=preset,
                chat_id=chat_id,
                path=path,
                user_text=args.once,
            )
        )
        # `ended` = a disposition closed the call (or the context budget tripped);
        # either way this conversation is archived and the next --once on this
        # --chat id starts fresh.
        print(reply + ("\n[conversation ended]" if ended else ""))
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")

    # Local import so `--help` doesn't require python-telegram-bot to be
    # importable (handy when students are mid-setup).
    from .telegram_bot import run_bot

    run_bot(token=token, preset=preset)


if __name__ == "__main__":
    main()
