# Integrating the medical-triage demo with KittenClaw

A proposal for the AI tutorial. It describes how to retire the GitHub Copilot
based triage harness in `04-multiagent-triage/task` and rebuild the demo on top
of the `KittenClaw` chat harness, then re-point the ReporterBot at the
conversations KittenClaw produces.

Style note: this proposal follows KittenClaw's house rule of plain ASCII
hyphens only (no en- or em-dashes), since most of the work lands in that repo.

---

## 1. What we have today

### 1a. The triage task (`~/agentic-ai-tutorial/04-multiagent-triage/task`)

A self-contained simulator built around a **shared filesystem queue**. Every
conversation is one YAML file in `conversations/`:

```yaml
id: alice
history:
  - $$HUMAN$$ Hi, I'm Alice Marsh, I'm 35 ... splitting headache for two days.
  - $$BOT$$ How severe is the pain on a 1-10 scale ...
last: HUMAN          # or BOT
# terminal markers, set by the triage bot when it closes the call:
# escalated: true
# scheduled: {date: 2026-03-04, time: am}
# no_further_action: true
```

Three kinds of actor read and write that queue:

| Actor | How it runs today | What it does |
|-------|-------------------|--------------|
| **Human** | `human-tools/` (Textual TUI, Telegram bridge, `reset.py` seeder, `status.py`) | Appends `$$HUMAN$$` lines, sets `last: HUMAN`. |
| **triage-bot** | a **GitHub Copilot skill** (`.agents/skills/triage-bot`) driving `message.py` | Polls for conversations where the human spoke last, replies, and *terminates* via `schedule` / `escalate` / `no-further-action`. |
| **reporter-bot** | a **GitHub Copilot skill** (`.agents/skills/reporter-bot`) driving `report.py` | Finds terminated conversations with no report yet, reads the full history, writes a structured intake form to `reports/<id>.yaml` (`name`, `sex`, `age`, `symptoms`, `triage` in {Minor, Moderate, Severe}). |

The pedagogy (from the task README) lives in the seams between these actors:

1. The triage-bot must gather data it does not itself need (name/age/sex) because
   the *reporter* needs it. (The classic "wrong info gathered" bug.)
2. Liveness vs safety: don't schedule unnecessarily; never fail to escalate an
   emergency.
3. Communicate effectively (it sometimes books a slot without telling the user when).
4. Validation / "backpressure": a classical validator in the tool catches
   whole classes of bad model output early.
5. Should we gather details *before* we escalate?

The important architectural fact: **the two bots never talk directly. They
coordinate through artifacts on disk** (the conversation files, then the report
files). That is the lesson worth preserving.

### 1b. KittenClaw (`~/KittenClaw`)

A deliberately tiny single-agent chat harness. One Telegram chat = one
conversation = one JSONL file under `conversations/`, where each line is a
literal wire-format message:

```jsonl
{"role": "system", "content": "..."}
{"role": "user", "content": "Hi, I'm Alice ..."}
{"role": "assistant", "content": null, "tool_calls": [{"id":"call_1","type":"function","function":{"name":"file_read","arguments":"{\"path\":\"skills/weather.md\"}"}}]}
{"role": "tool", "tool_call_id": "call_1", "content": "..."}
{"role": "assistant", "content": "How severe is the pain ..."}
```

Three runtime files, by hard rule (see `KittenClaw/CLAUDE.md`):

- `kittenclaw/__main__.py` - config, prompt rendering, JSONL I/O, the `turn_loop`.
- `kittenclaw/tools.py` - five tools (`web_fetch`, `web_search`, `file_list`,
  `file_read`, `file_write`), their JSON Schemas, and `dispatch()`.
- `kittenclaw/telegram_bot.py` - Telegram handlers, per-chat locks, the
  first-contact sticker + disclaimer, `/clear`, `/disclaimer`, `/about`.

Load-bearing invariants we must not break:

- The system prompt is rendered **once** when a conversation file is created
  (cache-friendliness). Skills inject only their frontmatter; the body is read
  on demand via `file_read("skills/<name>.md")`.
- **The conversation file *is* the messages list.** No sidecar metadata, no
  parallel schema.
- Terminal state = the file is moved to `conversations/archive/<name>` with its
  filename intact. `/clear` and the `max_context_tokens` auto-clear are the only
  things that archive today.
- All file tools are sandboxed under `workspace/` by `_safe_path`.

KittenClaw already *is* the Telegram front end and a real LLM agent loop, which
the original demo faked with `human-tools/telegram_bridge.py` plus a Copilot
session. That overlap is exactly what makes the integration a net simplification.

---

## 2. The integration thesis

> Make **KittenClaw the triage-bot**: a real model-driven Telegram agent whose
> persona *and* protocol are baked directly into one `SYSTEM.md`, and whose three
> triage outcomes are three new tools. A *completed* triage call becomes an
> **archived KittenClaw JSONL transcript that contains a disposition tool call**.
> The **ReporterBot stays a GitHub Copilot skill** whose only change is that its
> input adapter now reads KittenClaw transcripts instead of the old YAML queue.

Because the triage-bot is single-purpose, we take the radical simplification you
proposed: **delete the `workspace/` and `skills/` machinery entirely** and put
the one and only behaviour straight into the system prompt (see 3.1). That turns
KittenClaw into a smaller, sharper harness aimed at this one demo.

This keeps the part of the demo that teaches (two agents coordinating through
on-disk artifacts, plus the README lessons) while deleting the scaffolding (the
fake TUI/Telegram bridge/queue file format - KittenClaw's real Telegram loop
replaces all of it - and the skill/workspace indirection a one-behaviour bot
does not need).

### Data-flow, after integration

```
   Telegram user
        |  (text)
        v
+----------------------+        archives on disposition
|  KittenClaw          |   conversations/<chat>-<serial>.jsonl
|  = the triage-bot    | ------------------------------------------+
|  persona + protocol  |                                           |
|  baked into          |                                           v
|  SYSTEM.md           |                            conversations/archive/<chat>-<serial>.jsonl
|  + 3 disposition     |                            (terminal: contains a disposition tool call)
|    tools             |
+----------------------+
                                                                   |
                                                                   |  report.py next  (rewritten reader)
                                                                   v
                                                       +------------------------+
                                                       |  ReporterBot           |
                                                       |  external batch agent  |
                                                       |  (Claude Code/Copilot) |
                                                       |  SKILL.md + report.py  |
                                                       +------------------------+
                                                                   |  report.py report
                                                                   v
                                                          reports/<chat>-<serial>.yaml
                                                          (name, sex, age, symptoms, triage)
```

The triage-bot and the reporter never share memory or a process. Their entire
contract is: *"a completed triage call is an archived transcript with a
disposition tool call in it."*

---

## 3. Design detail

### 3.1 Triage-bot = KittenClaw (one baked-in prompt, no skills, no workspace)

Keep the **name "KittenClaw" and the `kittenclaw.webp` sticker** for branding,
as requested. Everything behavioural changes, and the harness gets smaller.

#### Is removing `workspace/` and `skills/` plausible? Yes, and better here

The skill mechanism (frontmatter in the prompt, body loaded on demand via
`file_read("skills/<name>.md")`) earns its keep when a bot has *many* behaviours
and you want discovery to be a small, observable, cache-cheap step. A triage bot
has exactly **one** behaviour. For a one-skill bot that indirection is pure cost:
an extra round-trip (`file_read`) at the start of every conversation, and a body
that is never reused across other skills. Baking the protocol straight into the
system prompt is **strictly more cache-friendly** here - the prompt is rendered
once when the conversation file is created and is then byte-identical every turn
(KittenClaw's existing invariant), so the protocol just lives in the warm prefix
with no discovery turn at all.

So the recommended shape is:

- **Delete `workspace/` entirely** - `SOUL.md`, `skills/`, `memory/`, the lot.
- The **persona and the full triage protocol both live in one file**, the system
  prompt itself.
- **The file tools go with the workspace.** `file_read`/`file_write`/`file_list`
  have no sandbox and no purpose once `workspace/` is gone (the triage bot reads
  no files and keeps no cross-call memory - each call is independent). Drop them,
  and drop `_safe_path` with them.
- The **skill-loading machinery** in `__main__.py` is deleted too: `load_skills`,
  `_load_skill_frontmatter`, `_FRONTMATTER_RE`, `load_workspace_file`, and the
  `skills=`/`load_workspace_file=` template inputs.

`web_fetch`/`web_search` are **removed too** (your call). They are independent of
the workspace, but a triage bot has no business browsing the web, and dropping
them teaches capability scoping by absence (README lesson #4a). `beautifulsoup4`
goes with them. What remains of the tool surface is exactly the **three
disposition tools** (3.2) plus the dispatcher - `tools.py` shrinks to roughly
those three functions, their schemas, and `dispatch()`.

#### No more Jinja: rename `system.md.j2` -> `SYSTEM.md`

Once the persona and protocol are static text and nothing is templated (no
`skills` loop, no `SOUL.md` include), **Jinja2 has no job left**. Per your call:

- Rename `system.md.j2` -> **`SYSTEM.md`** (plain markdown, no template tags).
- **Drop the `jinja2` dependency** from `pyproject.toml`.
- `render_system_prompt()` collapses to reading the file:

  ```python
  SYSTEM_PROMPT_PATH = ROOT / "SYSTEM.md"

  def render_system_prompt() -> str:
      """Return the system prompt verbatim. No templating: a single-purpose
      bot has one static prompt, rendered once into the conversation file."""
      return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
  ```

  (The name `render_*` is now a slight misnomer; `load_system_prompt` is fine if
  you prefer. Keeping the old name minimises churn in `new_conversation`.)

#### The shipped `SYSTEM.md` is deliberately unhelpful

Mirroring the original task's starter `triage-bot/SKILL.md` (which just replied
`🤐` and closed the call), the **`SYSTEM.md` checked into the repo is a
deliberately bad triage bot** - the broken starting point students are asked to
fix. It should be obviously inadequate but still *run*, so the loop, the tools,
and the reporter all work end to end while the behaviour is wrong. For example:

```markdown
# KittenClaw triage bot

You are a triage bot. When a patient messages you, reply with a single
shrug emoji (🤐) and then immediately call `no_further_action` to close the
conversation. Do not ask questions. Do not gather any details.
```

This guarantees students *see* the multi-agent pipeline work (a call closes, the
reporter picks it up) while every README lesson is visibly violated: no info
gathered (#1), emergencies not escalated (#3), nothing communicated (#4). Fixing
`SYSTEM.md` is the exercise.

#### The reference solution: `good.SYSTEM.md` (gitignored)

Ship the worked-good prompt as **`good.SYSTEM.md`**, listed in `.gitignore` so it
never lands in a student fork - the instructor keeps it locally to demo the
"after" against the students' "before" (drop it in over `SYSTEM.md` live). Its
content is the real triage protocol, encoding the README lessons as instructions:

- Persona: calm, careful, one focused question at a time, never diagnoses,
  always states the disposition (and, when scheduling, the exact date and time)
  back to the patient before closing.
- Collect over the chat: **name, age, sex, presenting symptoms, duration /
  severity, red-flag screen**. State explicitly that name/age/sex are needed
  *for the downstream intake form* even though triage itself does not use them
  (README lesson #1 - so students can later delete that line and watch the
  reporter starve).
- Decide exactly one disposition and call its tool:
  - **escalate** for emergencies (chest pain + arm numbness, uncontrolled
    bleeding, stroke signs). Safety rule: when in doubt, escalate; never gate
    escalation on having every field (README lesson #5; "gather-first" toggle
    discussed in 3.5).
  - **schedule_appointment** for non-emergent clinical needs; restate the booked
    date/time to the patient in the closing message (README lesson #3).
  - **no_further_action** for non-clinical contacts (opening hours, confirming
    an existing appointment).
- Liveness rule: do not schedule just to end the chat; book only when a visit is
  actually indicated (README lesson #2).

Doc impact: KittenClaw's own `README.md`/`SPEC.md` lean heavily on skills,
memory, and the workspace sandbox. Removing those means a real rewrite of that
prose - call it out as part of the work, not an afterthought.

### 3.2 Three disposition tools (the one real harness change)

Add three tools to `kittenclaw/tools.py`, wired the documented way (function +
`TOOL_SCHEMAS` entry + `_HANDLERS` row). They are intentionally **pure**: they
validate their arguments and return a confirmation string. They do **not** touch
the filesystem. Termination (archiving) is handled by the turn loop, which is
already the only place that archives - this respects "the conversation file is
the messages list" and keeps file mutation in one spot.

```python
# tools.py  (sketch)

VALID_TIMES = {"am", "pm"}

def schedule_appointment(date: str, time: str) -> str:
    """Book a routine appointment. Classical validator = backpressure:
    a malformed date is rejected here, the model sees the error in its
    tool message, and it retries - bad output never reaches the patient."""
    # YYYY-MM-DD, real calendar date.
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    if time not in VALID_TIMES:
        raise ValueError(f"time must be one of {sorted(VALID_TIMES)}, got {time!r}")
    return f"Appointment recorded for {date} ({time}). Conversation will close."

def escalate(reason: str) -> str:
    """Flag a possible emergency. No preconditions - safety must never be
    gated on data-gathering (README lesson #5)."""
    return f"Escalated to the emergency department. Reason: {reason}. Conversation will close."

def no_further_action(reason: str) -> str:
    """Close a non-clinical contact."""
    return f"Closed with no further action. Reason: {reason}. Conversation will close."
```

`schedule_appointment`'s validation is README lesson #4 made concrete: it rides
KittenClaw's existing dispatcher behaviour, where a raised exception comes back
to the model as `{"error": "ValueError: date must be YYYY-MM-DD ..."}` and the
turn keeps going, so the model can correct itself. Students can extend the
validator (reject past dates, clinic-closed days) and watch backpressure work.

The disposition (and, for scheduling, the date/time) lives in the tool **call
arguments** in the transcript. We deliberately do **not** make the tools take
`name`/`age`/`sex`/`symptoms` arguments: forcing those would auto-fix README
lesson #1 and rob students of the bug. The prompt *asks* for them; whether the
bot actually gathered them is left visible in the transcript for the reporter
(and the student) to discover.

### 3.3 Terminating the conversation (turn-loop change)

`turn_loop` already returns `(reply_text, auto_cleared)` and already archives on
auto-clear. We add a parallel terminal path. Three small edits:

1. Name the disposition tools in one place:

   ```python
   DISPOSITION_TOOLS = {"schedule_appointment", "escalate", "no_further_action"}
   ```

2. In the tool-dispatch loop, remember if any disposition tool was called this
   turn (one line where tool calls are iterated):

   ```python
   if tc.function.name in tools.DISPOSITION_TOOLS:
       disposition_called = True
   ```

3. At the "final reply" branch, terminate **after** the model has spoken its
   closing line, mirroring the existing auto-clear block:

   ```python
   if disposition_called:
       archive(path)          # terminal = archived, same as /clear & auto-clear
       return reply, True     # reuse the existing auto_cleared flag (your call)
   ```

Why terminate at end-of-turn rather than inside the tool: it lets the model emit
its human-facing closing message ("I've booked you for 2026-03-04 in the morning
- see you then") *before* the file is archived, so the closing line is part of
the saved transcript. That closing message is also README lesson #3 in action.

`telegram_bot.py` then sends a short closing notice, reusing the auto-clear
notice pattern, e.g. "This triage call is complete. Send a new message to start
another." Because termination archives the file, the patient's next Telegram
message naturally starts a fresh conversation (serial+1) - the right behaviour
for a new complaint, and it needs no new state.

Net harness footprint: ~3 tool functions + 3 schema entries + 3 handler rows in
`tools.py`, and ~4 lines in `turn_loop` plus a closing notice in
`telegram_bot.py`. No new modules, no abstractions - within KittenClaw's rules.

### 3.4 The completed-conversation contract

A conversation is **complete and reportable** iff:

- its file is in `conversations/archive/`, **and**
- its transcript contains an assistant `tool_calls` entry whose function name is
  in `DISPOSITION_TOOLS`.

This cleanly separates triage outcomes from the other two ways a file can land
in `archive/`:

- `/clear` (user housekeeping) - no disposition call, so not reported.
- `max_context_tokens` auto-clear (ran out of room mid-chat) - no disposition
  call, so not reported. The unfinished call simply never produces a report,
  which is correct.

The disposition and its parameters are recovered by reading the tool call:

| Tool call | Reported as |
|-----------|-------------|
| `escalate(reason=...)` | `escalated: true` |
| `schedule_appointment(date, time)` | `scheduled: {date, time}` |
| `no_further_action(reason=...)` | `no_further_action: true` |

### 3.5 ReporterBot = amended GitHub Copilot skill

The ReporterBot **stays a GitHub Copilot skill**, exactly as in the original
task: a `SKILL.md` plus a `report.py` tool, run by Copilot against the KittenClaw
repo. It is **not** a KittenClaw model skill (KittenClaw's model is the
patient-facing triage bot and must not also write reports). Place it at
`KittenClaw/.agents/skills/reporter-bot/` so it sits beside the data it reads.

Only the **reader half** of `report.py` changes. Concretely:

**`report.py next`** - rewrite to:

1. Glob `conversations/archive/*.jsonl` (sorted, stable).
2. For each, stream the JSONL (reuse the `jsonlines` dependency KittenClaw
   already vendors) and:
   - find the disposition tool call; skip the file entirely if there is none
     (not a completed triage call);
   - render the spoken transcript into the *same* `history` shape the reporter
     prompt already understands: `user` -> `$$HUMAN$$ <content>`,
     `assistant` with text -> `$$BOT$$ <content>`. Skip assistant messages whose
     `content` is null (pure tool-call turns) and skip `tool` result messages -
     the reporter only needs what was *said*.
   - attach the terminal marker from the disposition (table in 3.4).
3. Skip any conversation that already has `reports/<id>.yaml`, where
   `id = <chat>-<serial>` (the filename stem).
4. Emit the first match as YAML in exactly the format the reporter already
   consumes, then stop:

   ```yaml
   id: 481968615-001
   history:
     - $$HUMAN$$ Hi, I'm Alice Marsh, I'm 35 ...
     - $$BOT$$ How severe is the pain ...
     - $$HUMAN$$ About a 7, and the light really bothers me.
     - $$BOT$$ I've booked you for 2026-03-04 in the morning. See you then.
   scheduled:
     date: 2026-03-04
     time: am
   ```

   If none remain, print `# NO PENDING REPORTS` as before.

Because the output YAML is byte-compatible with what the old `report.py next`
produced, **the reporter's SKILL.md prompt barely changes** - it still "reads a
conversation, infers name/sex/age/symptoms, assigns a triage severity, and calls
`report`." That is the whole point of the word "amend": we adapted the input
adapter to KittenClaw's storage, not the agent's job.

**`report.py report`** - essentially unchanged: write `reports/<id>.yaml`,
require `name/sex/age/symptoms/triage`, validate `triage in {Minor, Moderate,
Severe}`. (The original validation logic is good as-is.) Note the division of
labour the demo teaches: the **triage-bot** chooses the *disposition*
(escalate/schedule/none); the **reporter** independently assigns a clinical
*severity* (Minor/Moderate/Severe) from the transcript. Mismatches between the
two (e.g. an escalated call the reporter rates "Minor") are a great discussion
prompt.

**`reporter-bot/reports/REFERENCE.md`** and **`SKILL.md`** prose: update to
point at `conversations/archive/` and explain the disposition-tool-call contract.

`reports/` lives at the KittenClaw repo root; add `reports/*.yaml` to
`.gitignore` next to the existing conversation ignores (they are per-student
runtime artifacts).

### 3.6 No human-tools

The original task's `human-tools/` (the Textual TUI `message.py`, the
`telegram_bridge.py`, `_common.py`, `reset.py`, `status.py`) are **dropped
entirely** - no one uses them, and KittenClaw's real Telegram bot already *is*
the human channel. There is also **no seed corpus**: completed conversations come
from actually driving the bot (via Telegram, or `--once` for a quick local
check). One less moving part to explain.

### 3.7 Keep `--once`: driving the agent from the CLI

KittenClaw already ships a no-Telegram path we **preserve for demos**:

```bash
uv run python -m kittenclaw --once "I have chest pain and my left arm is numb"
```

`--once` drives one user message through the *same* `turn_loop` the bot uses -
no Telegram token, no polling - and prints the reply to stdout. It reuses a fixed
debug `chat_id` (0), so successive calls **continue the same
`conversations/0-*.jsonl` thread**: you can play a whole triage exchange one line
at a time, then `cat` the JSONL to show the wire history (and `conversations/
archive/` once a disposition fires). Delete that file to start fresh.

This is how the instructor exercises a prompt without a bot token, and it is the
verification harness for build step 1 (bad prompt shrugs and closes; `good.
SYSTEM.md` escalates). The only requirement on our changes: `--once` must keep
working after we strip the skill/workspace machinery - it already calls
`render_system_prompt()` and `turn_loop`, both of which survive, so no extra work
is needed beyond not breaking it. Worth an explicit note because it is the
primary demo affordance now that the human-tools are gone.

---

## 4. File-by-file change list

### In `~/KittenClaw`

| Path | Action |
|------|--------|
| `SYSTEM.md` | **New** (renamed from `system.md.j2`): the deliberately-unhelpful starter triage prompt - persona + protocol baked in, no template tags. |
| `good.SYSTEM.md` | **New**: the worked-good reference prompt. Instructor-only; added to `.gitignore`. |
| `system.md.j2` | Remove (replaced by `SYSTEM.md`). |
| `workspace/` | **Remove the whole tree** - `SOUL.md`, `skills/` (incl. `weather.md`), `memory/`. |
| `kittenclaw/tools.py` | Remove `file_list`/`file_read`/`file_write` + `_safe_path` *and* `web_fetch`/`web_search`. Add the 3 disposition tools + schemas + handler rows + `DISPOSITION_TOOLS` set. |
| `kittenclaw/__main__.py` | Remove skill/workspace machinery (`load_skills`, `_load_skill_frontmatter`, `_FRONTMATTER_RE`, `load_workspace_file`, `SKILLS_DIR`/`WORKSPACE` paths). Collapse `render_system_prompt()` to read `SYSTEM.md`. `turn_loop`: track `disposition_called`, archive + return `auto_cleared=True` at end of turn. |
| `kittenclaw/telegram_bot.py` | Send a "triage call complete" notice on terminal (reuse the auto-clear notice path). |
| `pyproject.toml` | Drop `jinja2` and `beautifulsoup4` (no templating, no web tools). |
| `.gitignore` | Add `reports/*.yaml` and `good.SYSTEM.md`. |
| `.agents/skills/reporter-bot/` | **New** (ported + amended from the task): `SKILL.md`, `reports/report.py`, `reports/REFERENCE.md`. |
| `README.md` / `SPEC.md` (KittenClaw) | Rewrite the skills/memory/workspace sections - they no longer exist. |

### Dropped from the original task

The entire `human-tools/` tree (`message.py` TUI, `telegram_bridge.py`,
`_common.py`, `reset.py`, `status.py`), the YAML conversation format, the seed
corpus, and `.agents/skills/triage-bot/` (the triage bot is now KittenClaw
itself). The `textual` / `python-telegram-bot` / `watchdog` deps of the old task
go with them; KittenClaw brings its own Telegram client.

---

## 5. How the README lessons survive (and improve)

The lessons now live in `SYSTEM.md` (which students edit) and the reference
`good.SYSTEM.md`:

| Task README point | Where it lives after integration |
|---|---|
| 1. Triage gathers info only the reporter needs | `good.SYSTEM.md` asks for name/age/sex explicitly; remove that line and the reporter starves - visible in `reports/`. The shipped `SYSTEM.md` gathers nothing, so the bug is the default. |
| 2. Don't schedule unnecessarily (liveness) | Liveness rule in the prompt; observable in the transcript. |
| 3. Don't escalate an emergency? (safety) | `escalate` tool + prompt safety rule; a missed escalation shows as a `scheduled`/`no_further_action` transcript that should have escalated. |
| 4. Communicate the booked time | Closing message is emitted *before* archive, so it is in the transcript; reporter and student both see whether the time was stated. |
| 5. External tools / risk | `web_fetch`/`web_search` are removed: a triage bot has no business browsing the web. Teaches capability scoping by absence; the tool surface is only the three dispositions. |
| 6. Validation / backpressure | `schedule_appointment`'s date/time validator, surfaced through KittenClaw's existing tool-error path. |
| 7. Gather details before escalating? | A one-line toggle in the prompt (escalate-immediately vs ask-one-safety-question-first); compare transcripts. |

New lesson the integration adds for free: students can `cat` an archived
`.jsonl` and see the *exact* wire history - tool calls, arguments, results - that
led to a disposition. The old YAML hid the tool layer; KittenClaw exposes it. And
the deliberately-bad `SYSTEM.md` lets them watch the *whole pipeline* run end to
end while behaving wrong, then fix one file and see it come right.

---

## 6. Settled decisions

All settled per your direction - nothing left open:

- **ReporterBot stays a GitHub Copilot skill** (not a second KittenClaw loop).
- **Bake the one behaviour into the system prompt**; delete `workspace/` and the
  whole skill mechanism. Ship a deliberately-unhelpful `SYSTEM.md`; keep the
  reference `good.SYSTEM.md` gitignored.
- **Drop Jinja2**; rename `system.md.j2` -> `SYSTEM.md`.
- **Remove `web_fetch`/`web_search`** (and `beautifulsoup4`) along with the file
  tools: the tool surface is exactly the three disposition tools.
- **Reuse the existing `auto_cleared` flag** for termination (no new flag).
- **No human-tools, no seed corpus.**
- **Keep the `--once` CLI path** for driving the agent directly without Telegram
  (the primary demo affordance; see 3.7).
- **No patient fields as tool args** - keeps README lesson #1 (the
  gather-the-right-info bug) alive.

---

## 7. Build order (our work, done before class)

This is the integration work *we* do ahead of time, not a class activity. The
harness, tools, and pipeline ship finished. **The only thing students touch in
class is the prompt** - they are handed the deliberately-unhelpful `SYSTEM.md`
and learn by fixing it, watching the same loop, tools, and ReporterBot react.
That is the whole reason the bad prompt exists: there is nothing to assemble, so
the lesson is entirely about prompt-writing.

1. **Prompt + tools.** Write the deliberately-bad `SYSTEM.md` and the reference
   `good.SYSTEM.md`; add the three disposition tools; remove the file tools, the
   web tools, `_safe_path`, and the skill/workspace machinery; delete
   `workspace/`; drop `jinja2` and `beautifulsoup4`. Drive it with `uv run python -m
   kittenclaw --once "I have chest pain and my left arm is numb"` - with the bad
   prompt it should shrug and close; swap in `good.SYSTEM.md` and it should
   escalate.
2. **Terminal path.** `turn_loop` tracks `disposition_called` and archives at
   end of turn (`auto_cleared=True`); Telegram sends the closing notice. Confirm
   a completed call lands in `conversations/archive/` with the disposition tool
   call present.
3. **Amend the ReporterBot** (Copilot skill): rewrite `report.py next` to read
   archived JSONL and emit the familiar YAML; verify `report` writes
   `reports/<id>.yaml`. Update its `SKILL.md`/`REFERENCE.md` prose.
4. **Refresh prose:** KittenClaw `README.md`/`SPEC.md` (remove skills/memory/
   workspace), and the task README's lesson list.

Once these four are done, the in-class loop is just: student edits `SYSTEM.md`,
sends a message, reads the transcript and the resulting report, edits again.
