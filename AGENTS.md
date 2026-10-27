# AGENTS.md

shikiri-yaku is a background service that watches the escalations of Shikiru projects, sorts each
one into a category, hands it to a handler, and forwards what it cannot fix to the Product Owner
(PO) in Telegram. You are building it.

## Read first

1. `docs/design.md` is the specification. Section numbers below refer to it. When this file and the
   design differ, the design wins; report the difference in your result instead of choosing silently.
2. `tasks/plan.md` has your task list with acceptance criteria; `tasks/todo.md` is the checklist.
3. `docs/plan.md` is the effort estimate. You do not need it to implement a task.

Do not edit `docs/design.md`. If the design is wrong or silent about something you need, say so in
your report (or escalate); do not invent behavior.

## Commands

The quality gate is `uv run poe check`. It must pass before a task is done. Task 0 creates it from
Shikiru's gate: ruff (`select = ["ALL"]`), the comment rule, the literal rule, import-linter,
basedpyright, vulture and pytest. Use `uv run poe fix` to format.

## Architecture rules (design §12)

* Layout: `src/shikiri_yaku/` with `domain/` (pure, no I/O), `app/`, `extensions/` (one folder per
  category: its rules, handler and actions), `ports.py`, `adapters/`, `journal/`, `bootstrap.py`,
  `cli.py`. The repository folder is `shikiri-yaku`; the importable package uses underscores.
* Dependencies point inward. `domain` imports nothing else of the project. `app` and `extensions`
  import `domain` and `ports`. Adapters implement ports. Only `bootstrap.py` builds concrete objects.
  import-linter enforces this; never weaken a contract to make code pass.
* Everything a class needs comes through its constructor. No module-level state, no service
  locator, no "context" bag. Time comes from `Clock`, subprocesses from `CommandRunner`, Shikiru
  from `ShikiruClient`.
* Only the Router turns failures into `Forward`. Other code lets typed exceptions cross ports.
* SQL lives only in `journal/`. The journal is concrete SQLite; tests use an in-memory database.
* Every number is a setting with a default in the settings object (design §3.2), not a literal in
  handler code. Limits that belong to others (Telegram sizes, name validation) are named constants.
* A new category, handler, action, source, Reasoner job, classifier or channel is added by writing
  it and registering it in `bootstrap.py`. The Router, classifier and journal do not switch on a
  specific category or kind.

## Safety rules

* Principle 4: a model never writes a command. Code validates the model's typed fields and builds
  every host command from fixed recipes (design §6.3). Nothing runs on the host without the PO's
  approval, and what runs is the stored argument list.
* Never run package installs, `sudo`, or anything that changes the host from code you write for
  tests. Tests use fakes or harmless commands (`sleep`, `echo`) through `CommandRunner`.
* No live network in the gate. No real Telegram, no real agent CLI. The token file and secrets
  are never read by tests, logged, put in the journal, or committed.
* Every effect is journaled before it happens (principle 5).

## Tests

Write tests that protect a stated behavior, as listed in design §12 "Tests that earn their place".
Table-driven where the rule is a table. Fakes for ports; real SQLite in memory; recorded payloads
for Telegram. Do not write tests of message wording, getters, config defaults, or mocks of internal
classes, and do not chase a coverage number.

## Style

Validated data (config, Shikiru payloads, Telegram payloads) is parsed by pydantic models with
`extra="forbid"`; do not hand-write validators or table readers. Names come from the design's vocabulary: signal, category, extension, handler, action, outcome,
Reasoner, Channel, journal, op. Functions are short. Comments explain why, never what; follow the
comment rule. Docstrings only on public contracts. No code for a case the design does not describe.

## Working rules

* One task, one change, about 300 lines. If a task needs much more, stop and report how to split it.
* Update `tasks/todo.md` for the task you finished. Keep the edit to your own line.
* Never push, never touch another branch's work, never edit files outside this repository.
* If a pinned dependency or the quality gate fails for a reason unrelated to your change, say so with
  the evidence instead of working around it.
