# Reference

These tools are only for use by the `reporter-bot` skill.

Always invoke them from the root of the repository as the working directory.

## Reading the next unreported conversation

```sh
uv run .agents/skills/reporter-bot/reports/report.py next
```

Finds the first *finished* triage conversation that does not yet have a
corresponding file in `reports/`, and prints it as YAML:

```yaml
id: 481968615-001
history:
  - $$HUMAN$$ Hi, I'm Alice Marsh, I'm 35. I've had a splitting headache ...
  - $$BOT$$ How severe is the pain on a 1-10 scale, and ...
  - $$HUMAN$$ About a 7, and the light really bothers me.
  - $$BOT$$ I've booked you for 2026-03-04 in the morning. See you then.
scheduled:           # present if the bot scheduled an appointment
  date: 2026-03-04
  time: am
# escalated: true            # present instead, if the bot escalated
# no_further_action: true    # present instead, if the bot took no action
```

A conversation is "finished" when KittenClaw has moved it to
`conversations/archive/` **and** its transcript contains a disposition tool call
(`escalate`, `schedule_appointment`, or `no_further_action`). The `history` shows
only what was said; the disposition is summarised by the terminal key.

If no unreported finished conversations remain, prints:

```
# NO PENDING REPORTS
```

## Writing a report

```sh
uv run .agents/skills/reporter-bot/reports/report.py report "<id>" "<yaml text>"
```

Writes the report to `reports/<id>.yaml`. The YAML must include all of these
required keys:

| Key        | Description                                  |
|------------|----------------------------------------------|
| `name`     | Patient's name                               |
| `sex`      | Patient's sex                                |
| `age`      | Patient's age                                |
| `symptoms` | Description of presenting symptoms           |
| `triage`   | One of: `Minor`, `Moderate`, `Severe`        |

Additional keys are allowed. The tool exits with an error if any required key is
missing or if `triage` is not one of the three allowed values.

Example:

```sh
uv run .agents/skills/reporter-bot/reports/report.py report "481968615-001" "
name: Alice Marsh
sex: Female
age: 35
symptoms: Splitting headache for two days, light-sensitive, around 7/10
triage: Moderate
"
```

After a successful `report`, that conversation no longer appears in `next`
output.
