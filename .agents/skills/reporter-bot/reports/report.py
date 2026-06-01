"""Simulator tool for the reporter-bot skill.

Always run from the repository root:
  uv run .agents/skills/reporter-bot/reports/report.py <command> [args...]

The reporter reads the triage bot's *finished* conversations and writes a
structured intake form for each. A conversation is finished when KittenClaw has
archived it (moved it to conversations/archive/) AND its transcript contains a
disposition tool call (escalate / schedule_appointment / no_further_action).
Plain /clear and max-context auto-clears archive too, but without a disposition,
so they are skipped here.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

ARCHIVE_DIR = Path("conversations/archive")
REPORTS_DIR = Path("reports")

DISPOSITION_TOOLS = {"schedule_appointment", "escalate", "no_further_action"}


def _read_jsonl(path: Path) -> list[dict]:
    """Load a JSONL conversation file into a list of wire-format messages,
    skipping any blank or corrupt trailing line (same tolerance the harness
    uses on read)."""
    msgs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return msgs


def _disposition(messages: list[dict]) -> dict | None:
    """Return the terminal marker for the first disposition tool call in the
    transcript, or None if there is no disposition (so the conversation is not
    a finished triage call). The shape matches what the old YAML simulator
    produced, so the reporter prompt sees a familiar conversation."""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name")
            if name not in DISPOSITION_TOOLS:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "escalate":
                return {"escalated": True}
            if name == "no_further_action":
                return {"no_further_action": True}
            if name == "schedule_appointment":
                return {
                    "scheduled": {
                        "date": args.get("date"),
                        "time": args.get("time"),
                    }
                }
    return None


def _history(messages: list[dict]) -> list[str]:
    """Render the spoken turns into the `$$HUMAN$$` / `$$BOT$$` form the
    reporter prompt reads. Tool-call-only assistant turns (content null) and
    tool result messages are dropped - only what was actually *said* matters."""
    history = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and content:
            history.append(f"$$HUMAN$$ {content}")
        elif role == "assistant" and content:
            history.append(f"$$BOT$$ {content}")
    return history


def cmd_next(_: argparse.Namespace) -> None:
    """Print the first finished conversation that has no report yet."""
    if not ARCHIVE_DIR.exists():
        print("# NO PENDING REPORTS")
        return

    REPORTS_DIR.mkdir(exist_ok=True)

    for path in sorted(ARCHIVE_DIR.glob("*.jsonl")):
        messages = _read_jsonl(path)
        disposition = _disposition(messages)
        if disposition is None:
            continue  # archived but not a finished triage call
        conv_id = path.stem
        if (REPORTS_DIR / f"{conv_id}.yaml").exists():
            continue
        out = {"id": conv_id, "history": _history(messages), **disposition}
        print(
            yaml.dump(
                out, default_flow_style=False, allow_unicode=True, sort_keys=False
            ),
            end="",
        )
        return

    print("# NO PENDING REPORTS")


REQUIRED_KEYS = {"name", "sex", "age", "symptoms", "triage"}
TRIAGE_VALUES = {"Minor", "Moderate", "Severe"}


def cmd_report(args: argparse.Namespace) -> None:
    """Write a report YAML for the given conversation id."""
    REPORTS_DIR.mkdir(exist_ok=True)

    yaml_text: str = str(args.yaml_text).replace("\\n", "\n")
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        print(f"Error: invalid YAML - {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(parsed, dict):
        print("Error: report must be a YAML mapping", file=sys.stderr)
        sys.exit(1)

    missing = REQUIRED_KEYS - parsed.keys()
    if missing:
        print(
            f"Error: missing required keys: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        sys.exit(1)

    if parsed["triage"] not in TRIAGE_VALUES:
        print(
            f"Error: triage must be one of: {', '.join(sorted(TRIAGE_VALUES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    out_path = REPORTS_DIR / f"{args.id}.yaml"
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(
            parsed, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
    print(f"Wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reporter-bot simulator tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "next", help="Print the next unreported finished conversation"
    )

    p_rep = subparsers.add_parser("report", help="Write a report for a conversation")
    p_rep.add_argument("id", type=str, help="Conversation ID")
    p_rep.add_argument("yaml_text", type=str, help="Report contents as a YAML string")

    args = parser.parse_args()
    {"next": cmd_next, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
