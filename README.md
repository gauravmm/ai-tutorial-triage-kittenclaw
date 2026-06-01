# Multiagent Triage Bot Simulation

Patients will contact your chatbot on Telegram with their symptoms. You have to
develop two AIs: one that **triages** patients and schedules appointments, and
one that **reports** an intake document for each finished conversation.

Each agent has its own prompt and tools. Here the triage bot is KittenClaw
itself and the reporter is a GitHub Copilot skill, but in reality these would be
separate bots running in the cloud, coordinating only through the conversation
files they leave on disk.

## Triage Bot

The triage bot is **KittenClaw**, the Telegram bot in this repo. Its entire
behaviour is one file, `SYSTEM.md`. You need to make it:

1. Ask patients for their symptoms (not just rush to schedule an appointment).
2. Triage the patient. If the symptoms are:
    a. **Minor** (cuts and scrapes, runny nose, etc.): schedule an appointment.
    b. **Moderate** (fever, long-lasting cough, etc.): schedule an appointment.
    c. **Severe** (chest pain, heavy bleeding, etc.): tell them to go to the
       emergency room.

The bot closes every call with one of three tools: `schedule_appointment`,
`escalate`, or `no_further_action`.

### Getting Started

To start the bot, you need to:

1. Contact `@BotFather` on Telegram to create a new bot. Copy `.env.example` to
   `.env`, then paste the token in as `TELEGRAM_BOT_TOKEN=...`.
2. Get a free model API key from **one** of these providers (try them in this
   order) and paste it into `.env`:
    a. [Google AI Studio](https://aistudio.google.com/) as `GEMINI_API_KEY=...`
    b. [OpenCode Zen](https://opencode.ai/zen/) as `OPENCODE_API_KEY=...`
    c. [OpenRouter](https://openrouter.ai/) as `OPENROUTER_API_KEY=...`
3. Open the Run and Debug menu on the left (Ctrl + Shift + D). In the drop-down
   at the top, select the configuration for the provider whose key you set
   (`gemini`, `opencode`, or `openrouter`), then click the green Start arrow.
    a. Read the output in the terminal and look for `kittenclaw is up`.
4. Send the bot a message! You should get the kittenclaw sticker, a simulation
   disclaimer, and a reply.
5. Check the folder `conversations/` in the Explorer tab on the left. You should
   see your conversation as a `.jsonl` file. That file *is* the message history
   sent to the model, one message per line.

Unlike a coding-assistant setup, you do **not** re-trigger the AI by hand:
KittenClaw runs the model loop automatically on every message.

The bot will produce **unhelpful messages at first**: it shrugs at every patient
and closes the call without asking anything. Take a look at the prompt in
`SYSTEM.md` and improve it. After you edit it, send `/clear` in the chat so your
new prompt takes effect.

---

## Reporter Bot

Once the triage bot is working, you should then prepare a `reporter-bot` that:

1. Reads a finished conversation between a patient and the triage bot.
2. Extracts and reports the relevant details:
    a. `name`
    b. `sex`
    c. `age`
    d. `symptoms`
    e. `triage`, which is one of `Minor`, `Moderate`, `Severe`

This bot does not communicate over Telegram. It is a **GitHub Copilot skill** in
`.agents/skills/reporter-bot/`: it reads the archived conversations and saves
reports to `reports/`. Trigger it from Copilot and let it run the `report.py`
tool to find the next finished conversation and write its report.

Watch for the catch: if the triage bot never asked the patient's name or age,
the reporter cannot fill them in. The triage bot has to gather information it
does not itself need, because the *reporter* needs it.

## Going further

Once both bots work, try to break them, and then fix the prompt so they cannot:

1. **Liveness:** can you get the bot to schedule an appointment that was not
   needed?
2. **Safety:** can you get it to miss an emergency that should have been
   escalated? How would you mitigate that?
3. **Communication:** does it always tell the patient *when* the appointment is?
   It sometimes books a slot without saying so.
4. **Validation:** what happens if it tries to book an invalid date?
   `schedule_appointment` validates the date and rejects bad input, so the model
   has to correct itself. This is "backpressure", and a classical validator like
   it catches many bugs early.
5. **Tools:** should a triage bot be allowed to browse the web or run other
   tools? Is there a risk in that?
6. **Escalation:** if you already know you are going to escalate, should you
   still gather details first?

## Reading conversations

Conversations are JSON Lines, one message per line, and the file *is* exactly
what the model sees. Open one in the Explorer: the devcontainer installs the
**JSONL editor** extension (`toiroakr.jsonl-editor`), which shows each line as a
folded, readable message. That is the easiest way to trace why the bot did what
it did.

A conversation is **finished** once it moves to `conversations/archive/` with a
disposition tool call in it. That is what the reporter looks for.

## Advanced use

Prefer the shell? `uv run python -m kittenclaw --once "I cut my finger"` drives
one message through the bot without Telegram and prints the reply. Repeated calls
continue the same conversation, so you can play out a whole exchange one line at
a time. This is the quickest way to test a `SYSTEM.md` edit.
