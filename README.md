# <img src="kittenclaw.webp" alt="" height="40" valign="middle"> kittenclaw

A minimal chat harness for teaching agentic loops, set up as a **medical-triage
demo**. You talk to the bot on Telegram like a patient; it asks questions and
closes the call with one of three dispositions (book an appointment, escalate to
emergency, or no further action). A second agent, the **ReporterBot**, reads the
finished conversations and writes structured intake forms.

The whole point is the prompt. The bot ships **deliberately unhelpful** - your
job is to edit one file, `SYSTEM.md`, and watch the behaviour change. No code
required.

## How it fits together

Two agents that never talk directly. They coordinate through files on disk:

```
   you (on Telegram)
        |  "I've had chest pain for an hour..."
        v
+-----------------------+   archives on disposition
|  kittenclaw           |   conversations/<chat>-<serial>.jsonl
|  = the triage bot     | --------------------------------------------+
|  behaviour lives in   |                                             v
|  SYSTEM.md            |              conversations/archive/<chat>-<serial>.jsonl
|  + 3 disposition tools|              (a finished call: it contains a disposition)
+-----------------------+                                             |
                                                                      |  the ReporterBot
                                                                      v   (a GitHub Copilot skill)
                                                            reports/<chat>-<serial>.yaml
                                                            name, sex, age, symptoms, triage
```

The triage bot decides the *disposition*; the ReporterBot independently reads the
transcript and assigns a clinical *severity*. Getting them to cooperate (the
triage bot has to gather the name and age the ReporterBot needs, even though
triage itself does not use them) is half the lesson.

## The runtime

Three Python files, each answering one question:

| File                         | Answers                               |
| ---------------------------- | ------------------------------------- |
| `kittenclaw/__main__.py`     | how does the harness work end-to-end? |
| `kittenclaw/tools.py`        | what can the model do?                |
| `kittenclaw/telegram_bot.py` | how does Telegram plug in?            |

You can read all three in one sitting. There is no framework, no plugin system,
no hidden state - the control flow is right there in `turn_loop`.

## The one knob: `SYSTEM.md`

The bot's entire behaviour is its system prompt, a single markdown file at the
repo root. It is rendered into a conversation **once**, when that conversation
starts, and reused verbatim every turn after (this is what keeps the prompt
prefix cache-friendly).

The `SYSTEM.md` checked in is **intentionally bad** - it shrugs at every patient
and closes the call without asking anything. That is the starting point. You
improve it: teach it to gather symptoms, screen for red flags, escalate
emergencies, state the appointment time it books, and so on. Save the file,
start a fresh conversation (`/clear`), and the next message runs your new prompt.

There is no `skills/`, no `workspace/`, no memory directory. A triage bot does
one job, so its instructions live in one place.

> Instructors: a worked-good reference prompt lives in `good.SYSTEM.md`
> (gitignored, so it is not in student forks). Drop it in over `SYSTEM.md` to
> demo the "after".

## Tools

The model can call exactly three tools, all of which **end the conversation**:

| Tool                   | When                                                       |
| ---------------------- | ---------------------------------------------------------- |
| `schedule_appointment` | Non-emergency clinical need. Takes a date (`YYYY-MM-DD`) and `am`/`pm`. The date is validated - a malformed one comes back as an error the model has to fix. |
| `escalate`             | A possible emergency. Takes a short reason.                |
| `no_further_action`    | Non-clinical contact (opening hours, confirming a booking). Takes a short reason. |

When the model calls one, the bot lets it send its closing message to the
patient, then archives the conversation. The patient's next message starts a
fresh call.

## Quickstart (Codespaces)

1. **Fork** this repo on GitHub.
2. **Open in Codespace** - the devcontainer installs Python 3.14 + uv, runs
   `uv sync`, and copies `.env.example` to `.env`.
3. **Get a Telegram bot token** from [@BotFather](https://t.me/BotFather) and
   paste it into `.env` as `TELEGRAM_BOT_TOKEN=...`.
4. **Get a model API key** (free) - by default an
   [OpenCode Zen](https://opencode.ai/zen/) key as `OPENCODE_API_KEY=...`. See
   `kittenclaw.toml` for the other presets (OpenRouter, Gemini).
5. **Run** the bot:

   ```bash
   uv run python -m kittenclaw
   ```

   Then DM your bot on Telegram. The first message gets the kittenclaw sticker
   and a simulation disclaimer; after that you are talking to the triage bot.

No port forwarding, no public URL - Telegram long polling makes only outbound
connections, so a Codespace is fine.

> This is a teaching simulation, not a real medical service. Do not send real
> personal or medical information. In a real emergency, call your local
> emergency number.

## Local install

```bash
uv sync
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN + a model API key
uv run python -m kittenclaw
```

You need [`uv`](https://github.com/astral-sh/uv); it reads `.python-version` and
bootstraps Python 3.14 for you.

## CLI

```text
kittenclaw [--preset <name>] [--verbose] [--once "MESSAGE"]
```

- `--once "MESSAGE"` - **drive the agent directly, without Telegram.** Runs one
  message through the same `turn_loop` the bot uses and prints the reply. It
  reuses one debug conversation (`conversations/0-*.jsonl`), so repeated calls
  continue the same thread - you can play out a whole triage exchange one line at
  a time, then `cat` the JSONL to see exactly what happened. Delete that file to
  start over. This is the quickest way to test a `SYSTEM.md` edit:

  ```bash
  uv run python -m kittenclaw --once "I have chest pain and my left arm is numb"
  ```

- `--preset <name>` - pick a model preset from `kittenclaw.toml`. Defaults to
  `default_preset`.
- `--verbose` - per-tool-call debug logging on top of the one-line-per-turn
  token summary.

### What you'll see in the logs

After every model call, one line with the provider's token counts:

```
[chat 12345] turn 4  prompt=2843  completion=72  total=2915
```

`prompt` feeds the auto-clear check: when the next turn would no longer fit
`max_context_tokens`, the conversation is archived and the user is told to start
a new one.

## Commands

- `/clear` - archive this conversation and start fresh. The next message
  re-reads `SYSTEM.md`, so this is how your prompt edits take effect.
- `/disclaimer` - re-show the welcome sticker and simulation disclaimer.
- `/about` - what this bot is, and where the code lives.

## Conversations on disk

One file per conversation: `conversations/<chat_id>-<serial>.jsonl`. Each line is
a single chat message in the exact wire shape sent to the model:

```jsonl
{"role": "system", "content": "..."}
{"role": "user", "content": "I've had chest pain for an hour"}
{"role": "assistant", "content": null, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "escalate", "arguments": "{\"reason\": \"possible cardiac event\"}"}}]}
{"role": "tool", "tool_call_id": "c1", "content": "Escalated ..."}
{"role": "assistant", "content": "These could be signs of a heart attack - please call your emergency number now."}
```

The file read top to bottom **is** the messages array. `cat` it and you see
exactly what the model saw:

```bash
cat conversations/<chat_id>-<serial>.jsonl | jq -s
```

The devcontainer installs the `lehoanganh298.json-lines-viewer` VS Code
extension, which folds each line into a syntax-highlighted block.

A conversation is **finished** when it has been moved to
`conversations/archive/` *and* its transcript contains a disposition tool call.
(`/clear` and the auto-clear archive too, but without a disposition - those are
not finished triage calls and the ReporterBot skips them.)

## The ReporterBot

The ReporterBot is a **GitHub Copilot skill** in `.agents/skills/reporter-bot/`,
not part of the runtime. Point Copilot at it and it will:

1. Run `report.py next` to find the first finished conversation with no report
   yet, printing the transcript and its disposition.
2. Read it, infer the patient's name, sex, age, and symptoms, assign a triage
   severity (`Minor` / `Moderate` / `Severe`), and call `report.py report` to
   write `reports/<id>.yaml`.

If the triage bot never gathered the patient's age, the ReporterBot cannot fill
it in - which is exactly the kind of cross-agent bug this demo is built to
surface. `reports/` is gitignored runtime output.

## Teaching with kittenclaw

The whole in-class loop is prompt work, no code:

- Read the deliberately-bad `SYSTEM.md`. Run `--once` and watch it shrug.
- Edit `SYSTEM.md` to gather symptoms and pick a sensible disposition. `/clear`,
  message again, watch it improve.
- `cat` the JSONL: see the tool calls and arguments that led to a disposition.
- Try to break it: convince it to book an appointment that is not needed
  (a liveness failure), or to miss an emergency (a safety failure). Then fix the
  prompt so it cannot.
- Run the ReporterBot over your finished calls. Did the triage bot gather
  everything the report needs?

Things that need code (kept short on purpose): a new tool is a function plus a
schema plus a row in `_HANDLERS` in `kittenclaw/tools.py`; a new command is a
`CommandHandler` in `kittenclaw/telegram_bot.py`.

## Model presets

`kittenclaw.toml` holds the model presets. Each sets a `base_url`, `model`,
context budget, and an `api_key` that can reference an env var as `${VAR}`
(interpolated from `.env` at startup; an unset var fails loudly). Edit the file
to add a preset or change the default; switch at runtime with `--preset <name>`.

## License

MIT. Have fun.
