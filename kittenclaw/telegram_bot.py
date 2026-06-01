"""Telegram glue. python-telegram-bot handlers, the per-chat lock dict,
the first-contact disclaimer, the empty-response fallback, and long-message
chunking.

One Telegram chat = one kittenclaw conversation. Different chats interleave
on the asyncio event loop; the same chat is serialized by `asyncio.Lock`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import telegramify_markdown
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __main__ as harness  # ROOT, STICKER_PATH, paths, turn_loop, ...

log = logging.getLogger("kittenclaw.tg")

# Inline disclaimer - short enough that an external file would be ceremony,
# and keeping it next to the handler makes the teaching obvious.
DISCLAIMER = (
    "👋 Welcome to the kittenclaw triage simulator.\n\n"
    "This is a TRAINING SIMULATION, not a real medical service. Do not send "
    "real personal or medical information. In a real emergency, call your local "
    "emergency number now.\n\n"
    "Tell me what's going on and I'll help triage it.\n\n"
    "Commands:\n"
    "/clear - start a new conversation\n"
    "/disclaimer - re-show this message\n"
    "/about - what this bot is, and where the code lives"
)

# Inline like DISCLAIMER, for the same reason. Plain text (not markdown) so
# the URL is sent verbatim - no MarkdownV2 escaping to get wrong.
ABOUT = (
    "kittenclaw is a tiny teaching bot: a minimal agentic chat loop you can "
    "read in one sitting. Here it plays a medical triage assistant - it asks "
    "about your symptoms and closes the call by booking an appointment, "
    "escalating an emergency, or taking no further action.\n\n"
    "Source, spec, and docs: https://github.com/gauravmm/KittenClaw/"
)

# Telegram's per-message limit is 4096 chars. Leave headroom for markdown
# overhead; chunk at the last paragraph break before this boundary.
CHUNK_LIMIT = 4000


def _chunk(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split `text` into ≤`limit`-char pieces, preferring paragraph breaks
    near the boundary. Falls back to a hard slice when no break is in range."""
    out = []
    while len(text) > limit:
        # Prefer a paragraph break in the last ~25% of the window.
        cut = text.rfind("\n\n", limit // 2, limit)
        if cut == -1:
            cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


async def _send_disclaimer(update: Update) -> None:
    """Sticker + text, in that order. The sticker is the kittenclaw logo."""
    chat = update.effective_chat
    assert chat is not None
    try:
        with open(harness.STICKER_PATH, "rb") as f:
            await chat.send_sticker(f)
    except FileNotFoundError:
        log.warning("sticker missing at %s - skipping", harness.STICKER_PATH)
    await chat.send_message(DISCLAIMER)


async def _reply(update: Update, text: str) -> None:
    """Send a possibly-long assistant reply to the user. Empty text becomes
    the `(no content)` placeholder so the user knows the turn ended.

    Cheap models reliably emit GitHub-flavored markdown but botch raw HTML or
    hand-escaped MarkdownV2, so we let them write plain markdown and convert it
    here: `telegramify_markdown.markdownify` maps it to Telegram's MarkdownV2
    dialect and escapes the many characters MarkdownV2 treats as special (`.`,
    `-`, `_`, `(`, ...). We convert the whole reply *before* chunking - slicing
    raw markdown first could cut a code fence in half and unbalance it. Only
    this assistant text is formatted; the static status messages stay plain."""
    chat = update.effective_chat
    assert chat is not None
    if not text.strip():
        await chat.send_message("(no content)")
        return
    for part in _chunk(telegramify_markdown.markdownify(text)):
        await chat.send_message(part, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def on_disclaimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/disclaimer - re-show the welcome message. Does not alter state."""
    await _send_disclaimer(update)


async def on_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/about - introduce the bot and link to the source repo. No state change."""
    chat = update.effective_chat
    assert chat is not None
    await chat.send_message(ABOUT)


async def on_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear - archive the current conversation. The *next* user message
    starts a fresh file with serial+1 and re-renders the system prompt
    against the current skill set."""
    chat = update.effective_chat
    assert chat is not None
    locks: dict[int, asyncio.Lock] = context.application.bot_data["locks"]
    async with locks[chat.id]:
        path = harness.active_conversation_path(chat.id)
        if path is not None:
            harness.archive(path)
            await chat.send_message("✨ Conversation cleared. Send a message to start a new one.")
        else:
            await chat.send_message("(nothing to clear - no active conversation)")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The main entry point: a plain text message from the user."""
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None or not msg.text:
        return

    locks: dict[int, asyncio.Lock] = context.application.bot_data["locks"]
    client = context.application.bot_data["client"]
    preset = context.application.bot_data["preset"]

    async with locks[chat.id]:
        # First-contact disclaimer: send before we touch the conversation
        # file, so the test ("has this chat ever been greeted?") sees the
        # pre-message state.
        if not harness.has_ever_greeted(chat.id):
            await _send_disclaimer(update)

        # Ensure an active conversation file exists (a /clear leaves none).
        path = harness.active_conversation_path(chat.id)
        if path is None:
            path = harness.new_conversation(chat.id)

        try:
            reply, ended = await harness.turn_loop(
                client=client,
                preset=preset,
                chat_id=chat.id,
                path=path,
                user_text=msg.text,
            )
        except Exception:
            log.exception("[chat %s] turn loop failed", chat.id)
            await chat.send_message(
                "⚠️ Internal error - check the terminal for a traceback."
            )
            return

        await _reply(update, reply)
        # `ended` covers both a triage disposition and the max-context auto-clear:
        # either way the file is archived and the next message starts fresh.
        if ended:
            await chat.send_message(
                "✅ This conversation has ended. Send a new message to start another."
            )


# ---------------------------------------------------------------------------
# Entry point called from kittenclaw/__main__.py
# ---------------------------------------------------------------------------


async def _set_commands(app: Application) -> None:
    """Push the command menu to Telegram so the client's "/" list matches the
    handlers below. Telegram caches the last-registered list server-side, so
    without this a renamed or removed command lingers in the menu until
    someone clears it by hand. Keep these in sync with the CommandHandlers."""
    await app.bot.set_my_commands(
        [
            ("clear", "Archive this conversation and start fresh"),
            ("disclaimer", "Re-show the welcome message"),
            ("about", "What this bot is, and where the code lives"),
        ]
    )


def run_bot(token: str, preset: dict) -> None:
    """Build the PTB Application, wire handlers, and run long polling."""
    # post_init runs once after the bot is initialised but before polling, so
    # the menu is refreshed on every startup.
    app = Application.builder().token(token).post_init(_set_commands).build()

    # Per-chat locks. defaultdict means we get a fresh asyncio.Lock the
    # first time a chat ID is touched, no upfront registration.
    app.bot_data["locks"] = defaultdict(asyncio.Lock)
    app.bot_data["client"] = harness.make_client(preset)
    app.bot_data["preset"] = preset

    app.add_handler(CommandHandler("clear", on_clear))
    app.add_handler(CommandHandler("disclaimer", on_disclaimer))
    app.add_handler(CommandHandler("about", on_about))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("kittenclaw is up. Long-polling Telegram...")
    # run_polling blocks until Ctrl-C; it owns the event loop.
    app.run_polling(allowed_updates=Update.ALL_TYPES)
