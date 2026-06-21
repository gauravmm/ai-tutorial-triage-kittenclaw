"""The tools the triage bot can call. There are exactly three, and every one
of them *closes the conversation*:

- `schedule_appointment` - book a routine/non-emergency visit.
- `escalate`             - flag a possible emergency.
- `no_further_action`    - close a non-clinical contact.

Each validates its arguments and returns a short confirmation string. They do
**not** touch the filesystem - archiving the closed conversation is the turn
loop's job (`turn_loop` in `__main__.py`), the only place that moves files.

`DISPOSITION_TOOLS` lets the turn loop spot a closing call. The dispatcher
catches all exceptions and returns them as `{"error": "..."}`, so a bad argument
comes back to the model as a normal `tool` message it can correct.
"""

from __future__ import annotations

import datetime
import json

# The three tools that end a triage call. The turn loop watches for these.
DISPOSITION_TOOLS = {"schedule_appointment", "escalate", "no_further_action"}

_VALID_TIMES = {"am", "pm"}


# ---------------------------------------------------------------------------
# Disposition tools
# ---------------------------------------------------------------------------


def schedule_appointment(date: str, time: str) -> str:
    """Book a non-emergency appointment for `date` (YYYY-MM-DD) at `am`/`pm`.

    The date and time are validated here. This is the "classical validator" /
    backpressure idea: bad model output is rejected at the tool boundary and
    handed back as an error the model has to fix, instead of reaching the
    patient. `datetime.date.fromisoformat` rejects both wrong shapes
    ("next tuesday") and impossible dates ("2026-02-30")."""
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ValueError(
            f"date must be a real calendar date as YYYY-MM-DD, got {date!r}"
        )
    if time not in _VALID_TIMES:
        raise ValueError(f"time must be one of {sorted(_VALID_TIMES)}, got {time!r}")
    return f"Appointment recorded for {date} ({time}). The conversation is now closed."


def escalate(reason: str) -> str:
    """Flag a possible emergency. Takes a short `reason`. There are no
    preconditions on purpose: safety must never be gated on having gathered
    every detail first."""
    return (
        f"Escalated to the emergency department (reason: {reason}). "
        "The conversation is now closed."
    )


def no_further_action(reason: str) -> str:
    """Close a contact that needs nothing more (opening hours, confirming an
    existing booking). Takes a short `reason`."""
    return (
        f"Closed with no further action (reason: {reason}). "
        "The conversation is now closed."
    )


# ---------------------------------------------------------------------------
# JSON Schemas - passed to the model via chat.completions.create(tools=[...]).
# Described in prose in SYSTEM.md; the schemas are the API's concern.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_appointment",
            "description": "Book a non-emergency clinic appointment and close the conversation. Tell the patient the date and time before you call this.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Appointment date as YYYY-MM-DD.",
                    },
                    "time": {
                        "type": "string",
                        "enum": ["am", "pm"],
                        "description": "Morning (am) or afternoon (pm).",
                    },
                },
                "required": ["date", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Flag a possible medical emergency (the patient should go to A&E / call emergency services) and close the conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason for escalating.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "no_further_action",
            "description": "Close a contact that needs no clinical action (e.g. opening hours, confirming an existing appointment).",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason for closing with no action.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Map tool name → callable. All three are sync.
_HANDLERS = {
    "schedule_appointment": schedule_appointment,
    "escalate": escalate,
    "no_further_action": no_further_action,
}


async def dispatch(name: str, arguments_json: str) -> str:
    """Run a single tool call. Returns the string `content` the model will
    receive in its `tool` message. Any exception is caught and serialized as
    `{"error": "..."}` so the turn loop keeps going."""
    try:
        args = json.loads(arguments_json) if arguments_json else {}
        if name not in _HANDLERS:
            raise ValueError(f"unknown tool: {name}")
        result = _HANDLERS[name](**args)
        # The model expects a string; JSON-encode non-strings.
        return result if isinstance(result, str) else json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
