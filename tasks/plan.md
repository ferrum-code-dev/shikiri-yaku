# Task plan

The specification is `docs/design.md`; section numbers (§) refer to it. The effort estimate and the
phases are in `docs/plan.md`. Each task is about 300 lines of change, ends with `uv run poe check`
green, and leaves the repository usable. Tasks run one at a time, in the order below.

**Complexity** is the Leader's hint for the ladder: `high` marks tasks where a mistake loses data or
breaks the "every route ends at the PO" guarantee.

Phases B to E are outlined at the end and get detailed tasks after Phase A runs and the gate in
`docs/plan.md` §2.3 has been decided.

## Phase 0: setup

### T0. Scaffold with the quality gate (normal)

Create the Python project (uv, Python 3.14, `src/shikiri_yaku/`, `tests/`) and a gate equivalent to
Shikiru's: `poe check` runs format check, ruff with `select = ["ALL"]`, the comment rule, the literal
rule, import-linter contracts for the layers in `AGENTS.md`, basedpyright, vulture and pytest. Copy
the rule scripts and configuration from the Shikiru repository at `/Users/pavel/Projects/ferrum-code-dev/shikiru` (pyproject.toml,
`tests/support/comment_rule.py`, `tests/support/literal_rule.py`) and adapt names. Create the empty
packages named in `AGENTS.md`, `cli.py` with a `--version` command, and `.gitignore`.

Acceptance:
* `uv run poe check` passes on the scaffold, including a test for each rule script that proves it fails on a violating sample.
* An import-linter contract fails when `domain` imports `app`, `adapters` or `journal`.
* `shikiri-yaku --version` prints the version.

## Phase A: relay

### A1. Settings and instance directory (normal)

§3.1, §3.2. A frozen settings object loaded from `config.toml`: `product`, `components` (name,
path, optional `host` that is rejected with a clear message as "not supported yet"),
`channels.telegram`, `[operator]`, `[handlers.*]`, optional `[reasoner]` (stored, unused yet). Every
value except `product`, `components` and `channels.telegram` has a default. Resolve
`~/.shikiri-yaku/<product>/` (or `--home`), the token path relative to the instance directory, and
`instance.lock` acquisition with a clear error when held.

Acceptance:
* A valid file loads; a missing required key, an unknown key, a duplicate component name and a
  relative component path each fail with a message naming the key.
* The sample file in design §3.2 loads and matches the documented defaults.
* A second process cannot take `instance.lock`; the lock is released on exit.

### A2. Journal (high)

§9, §10. SQLite (WAL) with numbered migrations applied at start; tables `cursors`, `signals`,
`messages`, `ops` as described; small store classes with the operations later tasks need (record and
advance a cursor in one transaction, insert-if-absent a signal by id, transition signal and op state
with conditional updates, find non-final ops, find parked signals due). Signal numbers are row ids.
Only this package contains SQL.

Acceptance:
* Migrations create a fresh database and refuse a database newer than the code.
* Inserting the same signal id twice yields one row; the second call reports "already known".
* An op moves `intended → approved` only once under concurrent callers (test with two threads or tasks).
* Cursor advance and the signal rows of one batch commit atomically: a failure after the signal
  insert leaves the cursor unchanged (crash-point test).

### A3. Signals, classification and extensions (normal)

§4 (signal shape and ids), §5, §12. In `domain/`: `Signal` with the id pattern
`<project>:<kind>:<source id>`, `Category`, `Outcome` (`Resolved`, `Forward`, `WaitUntil`),
`Rule`, `Extension` (category, rules, handler, actions), the `Classifier` protocol and
`RuleClassifier` (first match wins in registration order, duplicate reason claims fail at
construction, the product catch-all is last). No handlers yet; the rule table of §5 is created from
small extension stubs in tests.

Acceptance:
* Table-driven test over every escalation reason Shikiru has (the 15 in §5 plus an unknown one):
  each maps to the category in the §5 table; the unknown one falls to `product`.
* `task_done` and `health` map to `notice`; `pr_review` to `review`; engine kinds to `infrastructure`.
* Two extensions claiming one reason raise at registration.
* A `decision_required` signal is classified by an injected async verdict function (the Reasoner
  comes later); no verdict function means `product`.

### A4. Ports, composition root and CLI (normal)

§12, §3.2. `ports.py` with `ShikiruClient`, `Channel`, `Reasoner`, `Classifier`, `CommandRunner` and
`Clock` protocols and the typed exceptions that cross them. `bootstrap.py` builds the object graph
from settings and is the console entry point: `main` hands the command line its composition, so the
command line never imports bootstrap. Only existing adapters are wired (the journal and the system
clock); no placeholder adapters are added.
CLI: `run <product>` (starts the asyncio loop, takes the instance lock, stops cleanly on SIGTERM) and
`doctor <product>` (validates config, paths, token file mode `0600`, lock, journal opens).

Split (decided): `install-service` is task A4b. A4 stays unchecked until A4b is done; the acceptance
below covers the part that lands first.

Acceptance:
* `run` with a valid config starts and stops on SIGTERM with exit code 0.
* `doctor` reports each failed check by name and exits non-zero; passes on a valid instance. A path
  of the wrong type (a directory where a file belongs, or the reverse) or one that cannot be read
  fails its own check and does not stop the others.
* `Clock` offers `now()` and `sleep(seconds)`; the gate bans `time.*`, `asyncio.sleep` and
  `datetime.now` elsewhere. The `CommandRunner` adapter and its subprocess exemption come with the
  Executor in phase D.

### A4b. Install service (normal)

§3.1, §12. `install-service <product>` writes a launchd plist (macOS) or systemd user unit (Linux)
named `shikiri-yaku-<product>`, with the PATH of the user's login shell and the exact `run` command.

Also: `doctor` shows a held instance lock as "running", not as a failure, because a service that is
up holds it; the check still fails when the lock file cannot be opened.

Acceptance:
* The generated unit/plist contains the login-shell PATH and the exact `run` command (golden file
  test on content that matters, not on whitespace).
* Both renderers are covered by tests that never write to the real launchd or systemd directories.

### A5a. Shikiru MCP link and read verbs (high)

§4, §10, §11. The `ShikiruClient` port grows its read verbs, with frozen pydantic result types in
`ports.py` or a module it imports: `wait_for_events(since_id, timeout_s)` returning events (id,
kind, task id, message, data, time), `status()` (engine up, quotas with `resets_at` and `used_pct`),
`list_escalations()` and `get_escalation(id)` (the escalation: id, task id, reason, headline, status,
resolution, plus the S1 fields `question`, `agent_options` with `label`, `consequences`,
`recommended`, `needs_input`, and `resolutions` with `kind`, `consequences`, `recommended`, and the
`packet` text), `list_tasks(statuses)` and `get_task(id)` (task id, parent id, status, title; only
the fields the design uses). The adapter in `adapters/` talks to one component's
`shikiru --project <path> mcp` over stdio with the `mcp` Python SDK (a pydantic-free thin wrapper:
call the tool, parse the structured content with the result types). It supervises the link: it
reconnects with backoff (`Clock.sleep`), and while the link is down every verb raises
`ShikiruLinkDownError`; a tool error raises `ShikiruRejectedError`; an unparsable result raises
`ShikiruProtocolError`. The executable is `shikiru` (found on PATH) and is a parameter, so the
contract script can point it at another checkout.

Acceptance:
* Unit tests with a fake MCP session cover each result parse, reconnect with backoff, the
  link-down error while disconnected, and the mapping of each exception.
* The result types accept a real S1 payload (use a JSON fixture recorded from the S1 branch, kept under `tests/fixtures/shikiru/`).

### A5b. Shikiru commands, `start` and the contract check (high)

§10.1, §11. The client port grows `resolve_escalation(id, kind, text|None)` (kinds `answer`, `retry`,
`accept`, `cancel`) and `submit_task(request)`; each returns an `Ack` (`done`, `rejected` or `queued`,
the command id, and for a submit the task id; check the real field names in the Shikiru repository
at `/Users/pavel/Projects/ferrum-code-dev/shikiru`, `operator/commands.py`, before you write the type).
`start_engine()` runs `shikiru --project <path> start` through the `CommandRunner` port. Write the
plain `CommandRunner` adapter here: an argument list (no shell), closed stdin, a timeout, the whole
process group killed on timeout, output captured; it is the one module exempt from the subprocess
ban (a per-file ignore in `pyproject.toml`); the Install phase adds the login shell and the host lock
on top of it. Add `scripts/contract_check.py`, run by a human and not by the gate: it starts a
real Shikiru engine with its fake agent in a temporary project, using the `shikiru` executable given
on the command line, and checks the assumptions of §11 through the adapter.

Acceptance:
* Unit tests with a fake MCP session: a `rejected` ack becomes `ShikiruRejectedError` with Shikiru's message; a `queued` ack is returned as queued.
* CommandRunner tests with real harmless commands: success output, nonzero exit code, timeout kills a child that ignores SIGTERM, stdin is closed.
* `doctor` gets one MCP probe per component (design §3.2): the link comes up and `status` answers. It is registered in `bootstrap.py` with this adapter.
* The contract script passes against a Shikiru that has S1 (the branch checkout at `/Users/pavel/Projects/ferrum-code-dev/shikiru`, run with `uv run --project`), and its output is recorded in the task report: events arrive, an escalation has `question`, `agent_options` and `resolutions`, a resolve is acknowledged and its command id appears in the `escalation_resolved` event, a second resolve is `rejected`, `start` brings `engine_up`. The script is not run by the agent unless it can start the fake agent offline; if it cannot, the task report says exactly what was not run.

### A6. Event source (high)

§4. One reader per component long-polling `wait_for_events(since_id)`: turns `escalation`,
`task_done`, `pr_opened` and fault-stop `engine_stopped` events into signals (escalation text from
`get_escalation`), closes signals on `escalation_resolved`, `pr_withdrawn`, `task_done` and
`pr_closed`, records lifecycle events in `cursors`. First sync: page all events keeping only
lifecycle state, create signals for open escalations, never replay history; a clean stop in the
history counts as respected. Repeat the first sync when the cursor falls behind retention.

Note: `CursorStore.respect_stop` does nothing for a project without a cursor row, so the first sync
records its batch before it respects a clean stop.

Acceptance:
* After a simulated crash between the signal insert and the cursor commit, a restart yields exactly
  one signal per source event.
* First sync of a project with history produces signals only for currently open escalations (open pull requests follow in A6b).
* `engine_fault` events of a running engine produce nothing; a fault-stop produces one `engine_fault` signal.
* `escalation_resolved` closes the open signal of that escalation, whoever caused it.

### A6b. First sync opens pull requests that are waiting (normal)

§4 "First sync". While paging the history, the first sync also tracks which pull requests are open:
a `pr_opened` with no later `pr_withdrawn`, `task_done` or `pr_closed` escalation for its task (the
escalation event's data names its reason). After the escalations, it creates a `pr_review` signal
for each open pull request, with the id and text of a live `pr_opened` event, so a later live read
of the same event is recognised by id.

Acceptance:
* First sync with a history of an open PR, a withdrawn PR, a merged PR and a closed PR produces one `pr_review` signal, for the open one.
* A restart after a first sync, and a repeated first sync after the cursor fell behind, add no second signal for the same PR.

### A7. Liveness and health sources (high)

§4. Liveness: every `liveness_interval_s` read `status.engine_up` and apply the table of §4,
including the clean-stop rule (respect after `stop_grace_min` when the stop happened during this
run; a never-respected earlier clean stop is `engine_down` at once; the respected event id is stored in `cursors`).
Health: an unreachable MCP link for `link_unreachable_min` opens one `health` signal and closes it on recovery.

Acceptance:
* Table-driven test of every row of the liveness table, including: reboot (clean stop before this run),
  deliberate stop respected after the grace time and still respected after an Operator restart, stop
  inside the grace time, project never started.
* `engine_down` ids are stable across restarts; one open `health` signal per outage.

### A8. Router, loop guard and scheduler (high)

§3, §6. The Router: classify, find the handler for the category, run it with a timeout, and turn an
exception, a timeout or an unknown category into `Forward`; handle each signal as an independent
asyncio task; `Forward` hands the signal to the product handler with the note. The loop guard
records automatic actions in `ops` and answers budget questions (`retry_budget_per_day`,
`restart_budget_per_hour`). The scheduler dispatches parked signals when due and sends due reminders
(`reminder_first_h`, `reminder_repeat_h`). `WaitUntil` parks the signal.

Note: `ops.identity` is unique, so every automatic action needs its own identity (for example
`retry:<project>:<task>:<reason>:<n>`), and the journal needs a query that counts the ops of a
`(project, task, reason, kind)` since a given time; add it to `OpStore` with its test.

Acceptance:
* A handler that raises, one that times out and an unregistered category each end as a forward carrying the failure in the note.
* A spent loop-guard budget returns a refusal; the same action for another task is allowed.
* A parked signal is dispatched once when due, also after a restart; reminders fire at the configured times and never twice.

### A9. Product handler (normal; needs S1)

§6.1. The message template over the structured escalation view: question, agent options with
consequences and the recommended mark, the forwarding note as "What I tried", reason hints from a small
table, buttons for options, free-text reply, `accept` and `cancel` with confirming taps. Signals
without an escalation (and failed notice, review or health messages) are shown as information with
*Acknowledge*. Messages go through the `Channel` port with callback data `a:<signal no>:<option no>`.

Acceptance:
* Given a fixture escalation view, the message contains the question, each option and one recommended mark; no raw packet markdown.
* An escalation without agent options offers only the resolutions the escalation allows.
* A message over 4096 characters is cut with a *Details* button; callback data is at most 64 bytes.

### A10. Telegram send and edit (normal)

§7. The `Channel` adapter outbound: `send(destination, message)`, `edit(message_ref, message)`,
destinations as the private chat of each owner, an outbox in the journal (a message is recorded
before sending and its delivery state after), size limits, retry on rate limit with the server's wait
time, refusal and deletion of messages that look like secrets (inbound check lives in A11).

Acceptance:
* Tests against recorded Telegram payloads (kept under `tests/fixtures/telegram/`): send, edit, rate limit, message too long, button data length.
* A crash between recording and sending leads to one resend carrying the signal reference (`S41`).

### A11. Telegram inbound and replies (high)

§7. Long polling with the offset saved only after the update is journaled; dedupe by update id;
allow-listed user ids only; reply and button routing by message purpose (`question`, `approval`,
`notice`) to a `Replies` service that issues `resolve_escalation` through the client (journaled as
an `ops` row first). Later answers to a resolved question get "Already resolved: … at …".

Acceptance:
* The same update delivered twice causes one resolve.
* A message from a user id not in `owners` is ignored and not stored.
* A tap on an approval message accepts only its own buttons; a free-text reply is matched by `reply_to_message`.

### A12. Command confirmation and resolved elsewhere (high)

§10, §10.1. Reconcile non-final `ops` at start; confirm `resolve` by the `escalation_resolved`
event carrying the command id; `rejected` shown to the PO with Shikiru's message; a `queued` resolve
whose escalation is still open `lost_command_min` after the engine is up again counts as lost and the
question is shown again; an `escalation_resolved` event the Operator did not cause closes the signal
and edits every copy of its message to "Resolved in Shikiru: …".

Acceptance:
* Crash-point tests: kill after queueing a resolve, after the acknowledgement, and mid-send, and check the restart outcome of each.
* A resolve done in tmux edits all copies of the open message and closes the signal.

### A13. Status, open, help and measures (normal)

§7 commands, §15 measures. `/status` (per component: engine up/down/never started, running, queued
and escalated tasks, quota blocks), `/open` (open escalations and pending approvals with their buttons
again), `/help`; free text gets the command list. A `shikiri-yaku stats <product>` command prints
PO wait time, handled-without-PO per category, the escalation mix and installs from journal timestamps.

Acceptance:
* `/status` and `/open` against a fake client and an in-memory journal produce the expected sections.
* `stats` on a seeded journal prints the four measures.

### A14. `/goal` (normal)

§7. `/goal <component> <text>` submits a task through `submit_task`, journaled as an op identified by
the Telegram update id; the PO gets the task id. After a crash between queueing and the acknowledgement,
the goal is not resubmitted: the PO is asked with the "I may have submitted this already" message
and a *Submit again* button.

Acceptance:
* A duplicate update id submits once.
* Crash-point test: a goal with an unconfirmed op is not resubmitted at restart and produces the question.

## Later phases (outline)

* **B Topics:** destination function for a forum group, startup topic reconcile, group allow-list,
  `/goal` without component inside a topic.
* **C Infrastructure basics and review:** retry, quota wait, engine restart, review handler, notice
  handler.
* **D Install:** Reasoner spike, interface, adapter and `assess_blocker`, ladder, validation and
  recipes, approval flow, Executor (with the `CommandRunner` adapter and its subprocess exemption in
  `pyproject.toml`), restart handling. `doctor` gets the Reasoner CLI probe (design §3.2) with the
  Reasoner adapter. The `assess` function given to
  `RuleClassifier` never raises: a Reasoner failure becomes `Assessment(OTHER, "Reasoner failed: …")`,
  which the classifier already sends to `product` with that note. `RuleClassifier` rejects two rules
  claiming one reason; relax that to (reason, verdict) pairs when a second category shares
  `decision_required`.
* **E Hardening:** drills, contract-test script, crash-point tests, security review.
