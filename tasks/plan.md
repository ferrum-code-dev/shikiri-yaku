# Task plan

The specification is `docs/design.md`; section numbers (§) refer to it. The effort estimate and the
phases are in `docs/plan.md`. Each task is about 300 lines of change, ends with `uv run poe check`
green, and leaves the repository usable. Tasks run one at a time, in the order below.

**Complexity** is the Leader's hint for the ladder: `high` marks tasks where a mistake loses data or
breaks the "every route ends at the PO" guarantee. From Phase B4 on, tasks are `normal` unless a
strong argument is written next to the task title.

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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
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

Acceptance criteria:
* First sync with a history of an open PR, a withdrawn PR, a merged PR and a closed PR produces one `pr_review` signal, for the open one.
* A restart after a first sync, and a repeated first sync after the cursor fell behind, add no second signal for the same PR.

### A7. Liveness and health sources (high)

§4. Liveness: every `liveness_interval_s` read `status.engine_up` and apply the table of §4,
including the clean-stop rule (respect after `stop_grace_min` when the stop happened during this
run; a never-respected earlier clean stop is `engine_down` at once; the respected event id is stored in `cursors`).
Health: an unreachable MCP link for `link_unreachable_min` opens one `health` signal and closes it on recovery.

Acceptance criteria:
* Table-driven test of every row of the liveness table, including: reboot (clean stop before this run),
  deliberate stop respected after the grace time and still respected after an Operator restart, stop
  inside the grace time, project never started.
* `engine_down` ids are stable across restarts; one open `health` signal per outage.

### A8. Router, loop guard and scheduler (split into A8a to A8d, each high)

§3, §6, §9. One task was too large; the Leader's split below was approved. Do one letter per change.
Each ends with `uv run poe check` and its own todo line. Keep `docs/design.md` unchanged.

### A8a. Loop guard and journal queries (high)

The loop guard records automatic actions in `ops` and answers budget questions: retries per
`(project, task, reason)` within `retry_budget_per_day`, restarts per project within
`restart_budget_per_hour`. `ops.identity` is unique, so every automatic action needs its own identity
(for example `retry:<project>:<task>:<reason>:<n>`), and the sequence must stay unique after a budget
window expires (do not derive it from the rolling-window count alone). Add the count query to `OpStore`
with its test. A refused action is recorded so the refusal history can be shown.

Acceptance criteria:
* A spent budget returns a refusal; the same action for another task is allowed; a restart budget counts across tasks and reasons.
* Identities stay unique after the window has passed.

### A8b. Router (high)

§3, §6. Classify, find the handler of the category in the registry, run it with a timeout, and turn an
exception, a timeout or an unknown category into `Forward`. Handle each signal as its own asyncio task.
`Forward` hands the signal to the product fallback, injected through the constructor (the real Product
handler is A9). `WaitUntil` parks the signal durably.

Note from the A8c review: the Scheduler (already merged, behind the `WakeTarget` port) claims a
due parked signal by moving it to `handling` and then awaits `wake`. The Router implements
`WakeTarget`; `wake` must never raise. A signal left in `handling` by a crash must be dispatched again
at start-up (find it in the journal), or a restart loses it.

Acceptance criteria:
* A handler that raises, one that times out and an unregistered category each end as a forward carrying the failure in the note.
* Signals run independently: a slow handler does not hold another signal.
* A parked signal is stored with its due time and the handler that parked it.

### A8c. Scheduler (high)

Dispatch due parked signals once, with a conditional claim, to the stored handler, also after a restart.
Reminders: `reminder_first_h`, then every `reminder_repeat_h`, bookkept as ops of kind `remind` with
one identity per signal and occurrence, so an occurrence is never scheduled twice. Delivery goes through
the journaled outbox; a recognizable duplicate after a lost send acknowledgement is allowed (§7).

Acceptance criteria:
* A parked signal is dispatched once when due, also after a restart.
* Reminders fire at the configured times and never twice for one occurrence.

### A8d. Source cleanup and failing sources (high)

Remove `LivenessSource.run` and its leftover dependencies; keep `events` required (`check` drains the
event source after a down status); move the run-loop tests to `ProjectSources`. A typed non-link
`ShikiruError` from a source (for example a malformed event) no longer ends the service: at the
`ProjectSources` boundary open one `health` signal at once for that source's failing episode, retry
after the existing delay without advancing the cursor, make no liveness decision while the event
synchronization is failing, and close that source's signal when it recovers. Link failures keep their
delayed health behavior.

Acceptance criteria:
* A malformed event opens one `health` signal, is retried, and does not crash the service; a later good batch closes the signal.
* No `engine_down` is opened while the event sync is failing.

### A8e. Dispatcher and start-up recovery (high)

§10 step 2. Nothing yet hands a new signal to the Router. Write the dispatcher: it takes the signals
in state `new` from the journal (oldest first) and gives each to `Router.submit`, and at start-up it
first puts every signal left in `handling` back to `new`, so a crash loses none. The Router no longer
lets a failing forward vanish: if `fallback.forward` raises (the Channel is down), the signal goes back
to `new` and is tried again after a delay setting (`forward_retry_s`), and the failure is visible in a
log line, not in an unobserved task. `Router.join` is for tests only.

Scope decision: `bootstrap.routing_jobs` composes the Dispatcher and Scheduler around a shared
Router. Calling it from `compose()` is deferred until A9's Product fallback and A10's outbound
Channel exist; A4 forbids placeholder adapters. Until then, real runs leave signals in `new`.

Acceptance criteria:
* A signal inserted by a source reaches its handler without any other call; two signals run concurrently.
* After a simulated crash a `handling` signal is handled again; a `resolved`, `forwarded` or `parked` one is not.
* A forward that fails once is retried and ends `forwarded`; the failure does not end the service.
* The dispatcher, the Router and the Scheduler are composed in `bootstrap.routing_jobs` as jobs;
  registering those jobs in `compose()` follows in A10 under the scope decision above.

### A9. Product handler (normal; needs S1)

§6.1. The message template over the structured escalation view: question, agent options with
consequences and the recommended mark, the forwarding note as "What I tried", reason hints from a small
table, buttons for options, free-text reply, `accept` and `cancel` with confirming taps. Signals
without an escalation (and failed notice, review or health messages) are shown as information with
*Acknowledge*. Messages go through the `Channel` port with callback data `a:<signal no>:<option no>`.

Acceptance criteria:
* Given a fixture escalation view, the message contains the question, each option and one recommended mark; no raw packet markdown.
* An escalation without agent options offers only the resolutions the escalation allows.
* A message over 4096 characters is cut with a *Details* button; callback data is at most 64 bytes.

Decisions (consultation 04-consult-i2; clarification to §7, `docs/design.md` not edited):
* Every escalation choice (options, answer, retry, accept, cancel, needs_input) uses `a:<signal no>:<option no>` with positive option numbers. `a` only selects a structured choice; input and confirmation are derived from that choice in A11.
* `d:<signal no>` is Details (packet parts) and `k:<signal no>` is Acknowledge (information-message behaviour of §6.1). A11 decodes `d` and `k` separately. A9 sends the `d` button on a cut message only and the `k` button on information messages only.
* Size: A9 stays one change (530 insertions, 15 deletions over 12 files). This is a task-specific exception to the ~300-line limit, not a precedent for later tasks.
* Recommended mark: on a recommended agent option when there is one; otherwise on a recommended resolution of a known kind.
* Option labels are numbers 1..n; Details appears only on truncation.
* A9 has no inbound handling and no production sender: `ProductForwarder` is not wired into `compose()`. A10 journals each outbound message before sending and tracks delivery per destination.

### A10. Telegram send and edit (split into A10a to A10d)

§7, §12. One task was too large (about 800 lines with tests); the Leader's split was approved. Do one
letter per change. Each ends with `uv run poe check` and its own todo line; `docs/design.md` stays unchanged.

### A10a. Recoverable outbox (high)

An `Outbox` port and a journal outbox: a message body is serialized and stored with its signal and
destination before anything is sent; delivery is tracked per destination; a restart sends what was
pending. `ProductForwarder` only queues; the Scheduler only books reminders (it stops calling
`Channel.send`); one sender drains every pending outbound row, reminders included. A crash between
recording and sending leads to one resend carrying the signal reference (`S41`).

Acceptance criteria:
* A crash after recording and before sending resends once; a confirmed row is never sent again.
* A reminder row and a question row are sent by the same sender; a failed send stays pending.

### A10b. Telegram send and edit (normal)

The `Channel` adapter outbound with an injected transport: `send`, `edit`, destinations as the private
chat of each owner, size checks (4096 characters, 64 bytes of button data), retry on rate limit with
the server's `retry_after` waited through `Clock`, and refusal of messages that look like secrets.
Decisions: secret patterns are, to start with, Telegram bot tokens, `sk-` keys, PEM private-key
headers, GitHub tokens and AWS access-key IDs (the exact forms are documented and tested; the list is
not exhaustive). The scan runs before a body is persisted and again at the adapter, so a secret never
enters the outbox. A refusal is terminal: it records safe metadata only, stops retries, and produces a
generic notice with the S-number and without the content; when even that cannot be delivered it is
reported on stderr as metadata only. Where an existing Telegram copy of a secret is identifiable, an
attempted deletion is journaled before it is invoked. Transient failures stay pending; a permanent
refusal is never treated as a delivery.

Acceptance criteria:
* Tests against fixture payloads shaped as the Bot API documents them (kept under `tests/fixtures/telegram/`, each file labeled with its source): send, edit, rate limit, message too long, button data length.
* Each secret form is refused and never reaches the journal or the transport.

### A10c. Composition and the source-to-delivery test (normal)

Shared Shikiru clients per project, the Product extension and the classifier registered in
`bootstrap.py`, `routing_jobs(...)` and the sender appended to `Composition.jobs`, using A9's fallback
and A10a/A10b's outbox and Channel. An integration test with fake external ports and no live network
shows that a signal created by a source is classified, forwarded, queued in the outbox and sent.

Acceptance criteria:
* A source-created escalation ends as a sent message with buttons, through the composed jobs.

### A10d. Bot API transport (normal)

Integration scope decision (A10c review): A10c includes a provisional `HttpBotApi` using stdlib
`urllib`, its bootstrap registration and `operator.telegram_call_timeout_s` (default 30 seconds).
A10d replaces that transport with the `httpx` implementation specified below, changes token loading
from once per call to once in the constructor, and replaces the loopback HTTP tests with
`httpx.MockTransport` tests so the gate needs no sockets. Reuse the existing timeout setting and
Channel wiring, and add the `getMe` doctor probe. A10d remains unfinished; its requirements and
acceptance criteria below still apply in full.

The real `BotApi` adapter: `call(method, payload)` posts JSON to
`https://api.telegram.org/bot<token>/<method>` and returns the decoded reply whatever its `ok` says;
no reply at all (connection error, timeout, a body that is not JSON) is a `ChannelError`. It reads the
`token_file` of the settings once, in its constructor, and the token is never logged, put in an
exception message, or kept anywhere else. Add the HTTP client as a dependency (`httpx`, pinned like the
others) and keep the call timeout a setting. `doctor` already checks the token file; add one probe that
asks `getMe` through this adapter. Register the adapter in `bootstrap.py` and give it to
`TelegramChannel` so the composed service can send. A11 will add `getUpdates` on the same adapter.

Acceptance criteria:
* With `httpx.MockTransport`: a documented success reply, a 429 reply with `retry_after`, a 400 reply and a connection error each come back as the Channel expects; the token appears in the request URL only.
* No test reads a real token file; the gate makes no network call.

### A11. Telegram inbound and replies (split into A11a, A11a2, A11b and A11c)

§7. One task was too large (700 to 900 lines with tests); the Leader's split was approved. Do one
part per change, about 300 lines each. Each ends with `uv run poe check` and its own todo line;
`docs/design.md` stays unchanged. The design does not specify `answerCallbackQuery` (acknowledging a tap); no change invents it.

### A11a. Inbound transport and parsing (high)

`getUpdates` through the shared Bot API, with typed taps, replies and texts retaining sender and
message references. No production polling or journal changes in this delivery (review 06-verify-i2).

Acceptance criteria:
* Documentation-shaped fixtures cover text, reply and tap references and polling arguments.
* Unsupported kinds can be skipped; malformed supported updates fail the whole batch.
* Protocol errors never expose inbound secrets in formatted tracebacks.

### A11a2. Durable inbound ingestion (high; deferred)

Reuse `messages` for update-id dedupe, sanitized content, source/reply references and pending/handled
state. Save allowed items and durable polling offset transactionally; filter non-owners before storage
without blocking progress. Apply the existing secret-refusal policy. Add the poller and timeout
settings (HTTP timeout must outlast polling). Production polling stays unwired until A11c.

Acceptance criteria:
* Duplicate delivery stores one item; non-owner content is excluded while its offset advances.
* Rollback preserves both rows and offset; pending items survive a restart until handled.
* Secret text is never persisted. In-memory SQLite tests cover these guarantees.
* Keep this change about 300 lines; stop and propose a further split if it exceeds that scope.

### A11b. Replies (high)

Routing by the journaled originating message and its purpose (`question`, `approval`, `notice`):
`a:`, `d:` and `k:` data decoded separately, free text matched by `reply_to_message`, `needs_input`,
and the confirming tap for accept and cancel (§6.1). An approval message accepts only its own buttons.
Choices are resolved by re-reading the escalation view, with the numbering rule shared with the product
renderer through an inward-facing helper. The typed resolve payload is journaled as an `ops` row with an
update-based identity before `resolve_escalation`, and the acknowledgement and command id are saved.

Acceptance criteria:
* The same update delivered twice issues one resolve, also after a restart.
* A tap on an approval message accepts only its own buttons; a free-text reply is matched by `reply_to_message`.
* An issued or uncertain op is left for A12; an acknowledgement alone never confirms a resolve.

### A11c. Polling, answers and composition (high)

Note from the A11a2 review (design §7 "Secrets never travel through chat"): an inbound item that
carries a secret is already stored as refused without its text; A11c must also delete that message
in the chat where the platform allows it (journal the attempt first), and a malformed supported
update, which fails its whole batch, must surface as one visible `health` signal instead of stalling
the polling in silence.

Polling and delivery of pending items registered in `bootstrap.py` with the shared Bot API and Shikiru
clients. "Already resolved: … at …" from the journal for a later answer; journaled edits that remove
the buttons from every copy once a resolution is observed. A12 keeps command-event correlation, start-up
reconciliation, lost commands and resolutions made elsewhere. `/status`, `/open`, `/help`, free-text help
and `/goal` stay with A13 and A14.

Order note: A12 is built before A11c2 to A11c4. A12 and A11c4 both edit every copy of a message to
remove its buttons or show its final state; whichever is built second reuses the mechanism of the
first and does not add another.

Acceptance criteria:
* An offline composition test runs an inbound tap through Replies to a journaled resolve.
* A later answer to a resolved question gets the recorded resolution and time.

### A12. Command confirmation and resolved elsewhere (split into A12a, A12b and A12c)

§10, §10.1. One task was too large; the Leader's split was approved. Build order: A12a, A12b, then
A11c2, A11c3 and A11c4, then A12c (it reuses A11c4's journaled edit-all-copies mechanism). One part per
change, each with `uv run poe check` and its own todo line; `docs/design.md` stays unchanged.

Facts from Shikiru (verified in `engine/handlers/escalations.py` and `domain/commands.py`): the
`escalation_resolved` event has `data` keys `escalation_id`, `resolution` (the kind: `answer`, `retry`,
`accept` or `cancel`), `command_id` (the id from the acknowledgement) and, for an answer only,
`text`. Declare only the keys you read (`extra="ignore"`).

### A12a. Rejected, queued and command-id confirmation (high)

Show `rejected` to the PO with Shikiru's message; keep `queued` or `done` on the op; confirm a resolve
when the `escalation_resolved` event carries the op's command id (ops move to confirmed).

### A12b. Start-up reconcile and lost commands (high)

At start every non-final resolve op is reconciled by observation: the escalation's state and events.
A `queued` resolve whose escalation is still open `lost_command_min` after the engine is up again is
lost: the op fails and the question is shown again (a new message, same signal). An op that was
claimed but never acknowledged is uncertain: the escalation's state decides; if it is still open, show
the question again. Crash-point tests: kill after queueing a resolve, after the acknowledgement, and
mid-send, and check the restart outcome of each.

### A12c. Resolved elsewhere (high)

An `escalation_resolved` event the Operator did not cause (no op carries its command id) closes the
signal and edits every copy of its message to "Resolved in Shikiru: *resolution*", using A11c4's
mechanism.

Acceptance criteria:
* A resolve done in tmux edits all copies of the open message and closes the signal.

### A13. Status, open, help and measures (split into A13a, A13b and A13c)

§7 commands, §15 measures. One task was too large; the split was approved in consultation 02-consult-i1.
Build order: A13a, then A13b (it extends A13a's command handling and the confirmation notice key), and
A13c, which is independent and reads the journal only. One part per change, each gated with
`uv run poe check`. Part status is kept here, not in `tasks/todo.md`; the A13 line there is ticked
only when all three parts pass. `docs/design.md` stays unchanged.

Status: A13a done (43a975b). A13b done after its consultation 06 follow-up (0561921). A13c done (141a9d0),
accepted at its size as a third explicit exception under consultation 10-consult-i1. A13 is complete and
its line in `tasks/todo.md` is ticked. Nothing is pushed.

Size: A13a (43a975b) is 459 changed lines including tests (443 added, 16 deleted), over the ~300
target. Consultation 04 accepted it as a one-time size exception; the commit is not rewritten. A13b
(7ffef86) is 481 changed lines including trackers (455 added, 26 deleted), about 463 excluding tracker
edits, against an estimate of 330 to 360 and the ~300 target. It is the second size exception, recorded
by consultation 06; the commit is not rewritten. The ~300-line limit still applies to later changes.
The follow-up that corrects 7ffef86 is a separate commit and is under that limit.
A13c (141a9d0) is 487 changed lines including trackers (472 added, 15 deleted): 464 are code and tests,
23 are tracker edits. The pre-edit estimate of 330 was too low, and the change is about 60% over the
~300 limit of consultation 08. Consultation 10-consult-i1 accepted it as a third, explicit size
exception. That does not excuse the estimate miss or grant a larger budget for later tasks. The commit
is not rewritten and was not split: the journal queries, calculations, reporting port and CLI
integration form one reviewable change.
A13c's journal read uses `Database.open`, which sets WAL and applies pending migrations, so a stats run
can write to the journal file. It is not a read-only open and is not described as one. Consultation
10-consult-i1 accepted this; a read-only open would need a new opening mode, which A13 does not add.

Rework after verdict 12-verify-i1 (changes_requested, 13-implement-i2): a command reply longer than one
Telegram message (/status with many components, the /open unreachable notice with many names) was queued
as one Message, and TelegramChannel refused it with TEXT_TOO_LONG, so the PO received only a refusal. Each
reply is now queued in parts of at most MESSAGE_TEXT_LIMIT, keyed `<key>:<index>` (`app/command_reply.py`,
`split_reply` in `domain/outbound.py`). The whole reply is screened before it is cut; a refused reply is
queued whole, so the existing refusal path notices the PO. A journaled logical-reply claim and all
parts commit in one transaction. A replay checks that claim before rendering `/status` again, so a
changed live state cannot append parts to the stored response; an interruption during insertion rolls
back the claim and every part together. The extra `command_replies` table stores only those claims; the
message parts remain in `messages` for delivery and restart recovery.
Regression: `tests/unit/test_command_parts.py` runs the real journal, outbox, sender and TelegramChannel
over FakeBotApi on an in-memory journal. FakeBotApi now numbers each sent message, since every part needs
its own platform message id. Size: 304 changed lines (289 added, 15 deleted) including the regression and
the new module, counted from `git diff` and the new files, at the ~300 target and before this note.

Correction after verdict 14-verify-i2: the first multipart version could append parts from a newly
rendered status when the same update was replayed after state changed. The journal now records a logical
reply claim with all bounded parts atomically; a replay after the queue commits but before inbound
handling completes drains the original stored parts. A failure partway through insertion leaves no
claim or partial rows. Regression tests reconstruct the command stack over the same journal after live
status changes and interrupt while enumerating the second part. This uses a small auxiliary journal table not
enumerated in design §9; it records the command idempotency needed to preserve the existing durable
outbox behavior without storing another copy of the reply text.

Design dependency: Reasoner rung status (§8, "`/status` and `doctor` show the state of each rung") waits
for the Reasoner phase. No rung is implemented yet, so no part of A13 shows rung status.

### A13a. Command intake, /help, free-text help and /status (normal)

Commands from the PO's private chat. `/status` and `/help`; any text that is neither a reply nor a
command, including `/start` and text-less messages, gets the command list. Replies and taps, sender
allow-listing, secret refusal, durable inbound deduplication and journal-before-send are unchanged.
Command replies have no signal: the outbound APIs take `None` for the signal number, and a refusal
notice for one names no signal and never renders "None". `/status` per component: engine up, down or
never started (never started: the engine is down and the lifecycle journal holds no engine event, design
§4), running, queued and escalated tasks from `list_tasks`, and quota blocks (windows at `used_pct` 100
or more, design §6.2). A component that does not answer is shown as unreachable; it does not stop the
inbound batch. Wired through `bootstrap.py`.

Acceptance criteria:
* A text reply to a question still answers and queues no command reply; `/status` and free text queue
  one reply each to the sender's chat, with no signal.
* The same update id delivered twice queues one reply.
* `/status` gives the engine state (up; down and never started; down after an engine event), task counts
  per status, quota blocks at 100 and not at 99, and an unreachable component does not hide the others.
* A signal-less message that carries a secret is refused, and its notice names no signal.
* Tests use fake clients and an in-memory journal and do not test exact wording.

### A13b. /open (normal)

Status: done after the consultation 06 follow-up (gate green, not pushed). The follow-up corrects two
defects of 7ffef86: an approval confirmed but not yet issued (op APPROVED) was not shown again by
`/open`, and a question was shown from a detail that was resolved or missing. The acceptance criteria
below are covered by tests/unit/test_open.py.

`/open` shows the open escalations (Shikiru's `list_escalations`) and the pending approvals (journal),
with their buttons again. Each copy is tied to its signal and, for an approval, its original op. A
replayed update re-shows nothing new; a re-shown copy never creates, approves or issues a second resolve
op. A minimal, validated extension of the confirmation notice-key parser is allowed; `confirm:<op id>`
and the callback-data rules stay. The click-time Shikiru check and the conditional op transition are
kept, and re-shown copies take part in final-state edits. Final or issued approvals and stale
escalations are excluded. Install approval belongs to Phase D; only approvals that exist today are shown.

Acceptance criteria:
* Replaying the same `/open` update re-shows nothing new.
* Two copies of one approval confirm one op: one resolve is issued.
* An escalation resolved elsewhere is not shown as open, and its tap does not resolve.

### A13c. Stats command (normal)

Status: done (141a9d0), accepted at 487 lines under consultation 10-consult-i1 (see A13, Size). The
acceptance criteria below are covered by tests/unit/test_stats.py. Gaps that remain by design: PO wait
is a journal-observed proxy; action attribution and the install outcome counters are unavailable until
recorded; misroutes are a manual review, not a printed measure.

`shikiri-yaku stats <product>` reads the journal only, reached through the composition root (`cli.py`
cannot import the journal). It starts no service job and touches no live Shikiru, Telegram or token file.
SQL stays in `journal/`. It prints four sections:
1. PO wait time, as labelled proxies. Each completed escalation (resolved or closed) gives a lifetime
   proxy, its created to updated time; unfinished signals are excluded. Its queue time is the earliest
   outbound question or approval, so copies and re-shows add no sample, and it is not proof the PO
   received it. Waiting is the final observed time minus that queue time, zero when nothing was queued.
   The share is the sum of the waiting times over the sum of the lifetimes, unavailable when that sum
   is not positive. It is not a mean of percentages.
2. Handled without the PO, per category: resolved signals with no outbound question or approval. Closed
   is not counted, since it includes external resolutions and cleanup. Zero until a handler returns
   Resolved (Phase C); action attribution is unavailable until actions are recorded.
3. Escalation mix: escalation signals by reason and by stored Reasoner verdict, an absent verdict shown
   as absent.
4. Installs: current-state install-op counts, labelled supplemental. The design's approved, declined and
   checks-failed outcomes are reported unavailable until Phase D.

Misroutes stay a manual review (design §15), not a fifth printed metric.

Acceptance criteria:
* `stats` on a seeded in-memory journal prints the four sections with proxies and gaps labelled.
* A closed signal with no question is not counted as handled without the PO; a copy is counted once;
  an unfinished signal is excluded from durations.

### A14. `/goal` (normal)

§7. `/goal <component> <text>` submits a task through `submit_task`, journaled as an op identified by
the Telegram update id; the PO gets the task id. After a crash between queueing and the acknowledgement,
the goal is not resubmitted: the PO is asked with the "I may have submitted this already" message
and a *Submit again* button.

Acceptance criteria:
* A duplicate update id submits once.
* Crash-point test: a goal with an unconfirmed op is not resubmitted at restart and produces the question.

Split (consultations 02 and 04, about 300 lines each; A14a ran about 386 lines):
* **A14a, durable submission core, not wired to `/goal`.** The submit payload, the `SubmitLog`
  port and its SQLite implementation, and `Submitter`. The op is journaled and claimed before
  `submit_task`, identified by `telegram:submit:<update_id>`. A task id (queued included) confirms,
  a rejection fails, and an issued op with no stored outcome is unknown and never sent again.
  Tests: duplicate update ids, acknowledgement persistence, a crash after the call.
* **A14b1a, retry reservation (consultation 04).** A tap on *Submit again* consumes its unknown
  submit and intends the replacement under the tap's update id, in one transaction; the unknown
  outcome stays on the original op. A replayed tap gets its replacement, and consumed prompts are
  left out of `unfinished()`. Tests: duplicate delivery, a second update, a tap that names no
  unknown goal, a crash between intent and send.
* **A14b1b, `/goal` command.** Parsing, usage and help; the saved task id and a rejection through
  the outbox; the uncertain question with its *Submit again* button; tap routing validated against
  the stored originating message, channel and chat. Production wiring is in A14b2.
* **A14b2, wiring and startup recovery.** `/goal` in `Commands`, the tap in `Replies`, and the
  composition; startup reconciliation of unfinished submit ops before the sources start. Replay and
  recovery share response deduplication. The acceptance points above are tested here, offline, by
  a restart test.

## Phase B: topics

Design §7 and §13 "Telegram topics". Only the destination function and a start-up reconcile change;
handlers, Router and the outbox contract stay as they are. The private chat of each owner keeps
working when no group is configured.

### B1. Destinations with topics (normal)

`[channels.telegram]` gets an optional `group_id` (a forum supergroup). A `Destination` gains an
optional `thread` (the topic id). The destination function picks the target of a signal: with a group,
the topic of the signal's component for escalations and the one general topic for every other
signal; without a group, the private chat of each owner as now. The topic ids are kept in the journal
(`topics`: component or `general`, thread id, state). `TelegramChannel` sends with `message_thread_id`
and the outbox stores the thread with the destination. Message references keep working for edits.

Acceptance criteria:
* An escalation of component `api` is queued for the `api` topic, a `task_done` notice for the general topic, with a group; both go to the owners' private chats without one.
* A restart sends what is pending to the same topic (the thread is stored with the row).

### B2. Topic reconcile at start-up (normal)

At start, with a group: create a forum topic for each component and one general topic that is missing
in the journal (`createForumTopic`), and close the topic of a removed component (`closeForumTopic`),
keeping its history. `doctor` checks that the bot is an administrator with the manage-topics right
and that the group holds only the owners and the bot (everyone in the group sees project data), and
names the problem when not.

Acceptance criteria:
* With fixtures shaped as the Bot API documents them: a new component gets one topic; a second start creates nothing; a removed component's topic is closed and not deleted.
* `doctor` fails on a group with a non-owner member.

### B3. Inbound in topics (normal)

Updates from the group are served only for the configured `group_id` and the owners; an update from
another chat is ignored and not stored. A reply or tap in a topic is routed by the originating message
as before. `/goal <text>` inside a component's topic implies that component; `/goal` in the general
topic still needs the component name.

Acceptance criteria:
* A message from a group that is not configured is ignored and not stored.
* `/goal <text>` in the `api` topic submits to `api`; in the general topic without a component it asks for one.

### B4. Telegram topics belong to the adapter (normal)

Design §13 puts topics in the Channel adapter ("destination function + startup reconcile"). B1 to B3
put the policy in `app` and `bootstrap`. Move it, without changing behavior, so that a second channel
needs no edit in the core. The core keeps `Destination(channel, chat, thread)` and `Inbound.chat` and
`.thread`; those are generic.

Target shape:
* `ports.py` gets one small port, `Destinations`, with three operations: the destinations of a signal (project, kind), the component an inbound item implies (or None), and an async `prepare()` that the channel runs at start-up. `GoalCommand` and the forwarder use only this port.
* The adapter package owns `ForumTopics`, `TopicReconciler`, the topic journal protocol and record, the `TOPIC_*` states, the destination function (including the private owner chats without a group and the "topic is not open" failure), and the rule that only the owners' private chats and the configured group are served. The reconciler moves from `app/` to `adapters/`.
* `journal/topics.py` stays the only SQL; the adapter reaches it through a protocol declared in the adapter module. No import-linter contract is weakened and no new exemption is added.
* `TopicCreationUncertainError` becomes a channel-neutral start-up error in `ports.py` (the Telegram wording stays in the message the adapter raises); `cli.py` catches that one.
* `InboundPoller` loses `group_id`: the chat rule runs in the adapter, and an unserved update still moves the offset without being stored. The owner check may stay in the poller.
* `bootstrap.py` only builds the adapter pieces and passes them in.

Acceptance criteria:
* `grep -rniE "topic|forum|group_id|supergroup" src/shikiri_yaku` (SQL `GROUP BY` and ordinary words are not a match) matches only under `adapters/`, `journal/topics.py` and its migration, `settings.py` (`group_id`), and `bootstrap.py` wiring lines. Nothing in `app/`, `domain/`, `extensions/`, `ports.py` or `cli.py`.
* Every existing B1 to B3 test still passes with only its imports and constructor arguments changed; no test is deleted.
* `/goal <text>` in a component's topic, the topic reconcile, the uncertain-creation stop and the group filter behave as before.

## Phase C: infrastructure basics and review

Design §5 and §6.2 to §6.5. All tasks are `normal`. Each extension is a folder under `extensions/`
registered in `registered_extensions()` before `product`; the catch-all stays last. Handlers get no
signal number, so a message a handler sends itself is queued with `number=None` and a key built from
`signal.id`, through the `Notifier` that C1 adds. `decision_required` with verdict `host_tool` belongs to
Phase D and is not registered here, so it keeps going to `product`.

### C1. Infrastructure handler and Retry (normal)

`extensions/infrastructure/`: the rules of design §5 (the eight escalation reasons and the kinds
`engine_down` and `engine_fault`), an `InfrastructureHandler` that picks the registered action by the
signal's reason or kind from a dictionary built in its constructor (a signal no action covers
forwards), and the **Retry** action for `agent_error`, `agent_stuck`, `agent_protocol`,
`integration_conflict` and `integration_blocked`: ask the loop guard, then
`resolve_escalation(retry)`. A refused guard forwards with its history. Two actions claiming one reason
fail at registration. Add `app/notifier.py`: `Notifier.tell(signal, text, key)` queues an informational
message for the signal's destinations with `number=None`; the key is recorded so a repeat sends nothing.

Acceptance criteria:
* Each of the five reasons calls `resolve_escalation(retry)` once and returns `Resolved`; the second escalation of the same task and reason within a day forwards, and the note names the earlier retry.
* An escalation of a reason no action covers (for example `ladder_exhausted` before C2) forwards with a note that names it.
* End to end with the in-memory journal and a fake client: an `agent_error` signal reaches `Resolved` and nothing is sent to the PO.

### C2. Quota wait (normal)

The **Wait** action for `quota_exhausted` and `ladder_exhausted`: the reset time is the latest
`resets_at` among the windows of `status().quotas` whose `used_pct` is 100 or more. Tell the PO the
time (through the Notifier), record a `wait` op and return `WaitUntil(reset + 1 minute)`. On the second
call the op is due, so the action performs `resolve_escalation(retry)` and returns `Resolved`. No
known reset time, or one more than `quota_wait_max_h` away, forwards.

Acceptance criteria:
* Table-driven: no exhausted window, an exhausted window without `resets_at`, a reset beyond the maximum all forward; a reset in 3 hours parks until then plus one minute and sends one notice.
* The second call after the wake time resolves with `retry` and sends no second notice; a restart between the two calls does the same (the op decides, not memory).
* A wake time in the past after a long outage resolves at once.

### C3. Engine restart (normal)

The **Restart** action for `engine_down`, `engine_fault` and `engine_error`: ask the loop guard, call
`start_engine()`, then confirm `status().engine_up` (retrying for `restart_confirm_s`, a new setting
with a default) and tell the PO. For `engine_error` restart only if the engine is down, then
`resolve_escalation(retry)`. A refused guard, a failed start or an engine that stays down forwards with
the failed checks from `shikiru doctor --json`, run through `CommandRunner`.

Acceptance criteria:
* A down engine that comes up returns `Resolved` and sends one notice; the second restart within an hour forwards with the first restart's time and the doctor's failed checks.
* A start that exits nonzero forwards with Shikiru's message; an engine that never comes up forwards after `restart_confirm_s`.
* `engine_error` with the engine up calls only `resolve_escalation(retry)`.

### C4. Review handler (normal)

`extensions/review/`: the rule for the `pr_review` kind, a `ReviewHandler` that sends the message of
design §6.4 and returns `Forward`-free outcomes of its own: the title from `get_task`, the commit count
and the link from the signal, an *Open PR* link button (a `Button` gains an optional `url`; a button
has `data` or `url`, never both, and the Telegram adapter sends `url` as a link button) and an
*Acknowledge* button that silences the reminders. The message is sent with the signal number so the
reminders of A8c apply. Read what the event source already keeps of `pr_opened` and add only what is
missing to the signal's `text`; report if a signal field is needed.

Acceptance criteria:
* A `pr_review` signal produces one message with the link button and *Acknowledge*; a restart does not send it twice.
* An unanswered review is reminded by the Scheduler on the Product handler's schedule, and *Acknowledge* stops the reminders.
* If `get_task` fails the message is still sent, with the task id as the title.

### C5. Review closing (normal)

The review message is edited to its final state and its reminders stop when the task is done
(merged), the PR is withdrawn, or Shikiru escalates `pr_closed`. Reuse the final-edit path of A11c4 and
the closing rules of the event source; extend them only where `pr_review` is not covered yet, and
report any gap in the design instead of inventing behavior.

Acceptance criteria:
* Each of the three events edits every copy of the review message to its final state, with the buttons removed, and a late *Acknowledge* tap changes nothing.
* A `pr_closed` escalation still reaches the Product handler as its own signal.

### C6. Notice handler (normal)

`extensions/notice/`: the rules for the kinds `task_done` and `health`. On `task_done`, read
`list_tasks`, follow `parent_id` to the family root and stay silent while any task of that family is
`queued`, `running`, `integrating` or `escalated`; when none is left send one message per root:
"api · *root title* integrated (N tasks)", keyed by the root so it never repeats. A `health` signal
becomes one message that is edited to "recovered" when the signal closes.

Acceptance criteria:
* A family of three tasks produces one message, after the last one is done, with the count 3; replaying the events produces none.
* A task with no children produces one message at once.
* A `health` signal is sent once, and its closing edits the message; the Notice handler failing forwards the original signal (the Router's rule).

### C7. Phase C paths through the composed app (normal)

`tests/unit/test_composition.py` runs one escalation from the Shikiru source to a sent message and one tap to a
resolve. The Phase C paths are tested only one part at a time. Add composed tests, in the same style
(`compose()` with the fake Shikiru session, the fake Bot API and `NapClock`), for three paths. Build each
escalation in the test from the recorded fixture by changing its reason; add a fixture file only if that
is not enough. Change production code only if a test shows a defect, and report the defect instead of
working around it.

* **Retry:** an `agent_error` escalation is resolved by the Operator with `resolve_escalation(retry)`, once, and
  the PO gets no message.
* **Quota wait:** a `quota_exhausted` escalation with an exhausted window makes the PO notice and no
  resolve; when the clock passes the reset time plus the margin, `resolve_escalation(retry)` is sent once
  and the notice is not sent again. The test needs a clock whose `now()` can be moved (extend
  `tests/support/clock.py`); never wait in real time.
* **Review:** a `pr_opened` event sends one review message with an *Open PR* link button and
  *Acknowledge*; the task being done edits that message to its final state and removes the buttons.

Acceptance criteria:
* Each of the three tests fails when the part it exercises is broken: show it for one deliberate break per test (for example, leave the Router's `Resolved` handling out, skip `WaitUntil`, skip the final edit) and say which break you used in the report.
* The suite stays under 10 seconds and no test waits on a real interval.
* The gate passes.

### C8. Fixes from the first live run (normal)

The Operator was run for the first time against a real Shikiru project and a real Telegram bot. Four
defects showed. Design §6.1 now describes the header (**Header** bullet). Do not edit `docs/design.md`.

1. **A refused `getUpdates` is reported as unreadable.** The bot had a webhook, so Telegram answered
   `{"ok":false,"error_code":409,"description":"Conflict: can't use getUpdates method while webhook is
   active; use deleteWebhook to delete the webhook first"}`. The update parser accepts only `ok: true`, so
   the health message said "the Bot API replied in a shape the Channel does not read". Read the failure
   reply (as the other Bot API calls already do with `_Failed`) and raise the error with the code and
   the description. A reply that is neither shape stays "unreadable".
2. **`doctor` does not see a webhook.** Add a Telegram check with `getWebhookInfo`: a non-empty `url` fails
   and says a webhook is set and the host it points at. Never print the rest of the URL.
3. **Header of a decision message.** Today `S2 · shop · api · T-1 · Decision needed`. Make it
   `T-1 · <title> · S2` in a component's topic and `api · T-1 · <title> · S2` anywhere else (General
   topic, private chat). The title is `get_task(task_id).task.title`, cut to a setting (a new
   `message_title_max_chars`, default 80); when `get_task` fails or the signal has no task, the header has
   the task id only. The product name is no longer shown. An information message keeps "For information"
   after the title. Whether a destination is a component topic comes from `Destinations`
   (`component_of`); if the message is built once for all destinations, build it per destination. If this
   needs much more than 100 lines, stop and report how to split it.
4. **A health message was not edited when its signal closed.** In the live run the signal
   `operator:health:telegram_inbound_batch:...` was `closed` after the Operator restarted and the source
   worked, but its message stayed as it was: `final_edited = 0` and no `final_body` in the journal. C6 says it
   becomes "Recovered". Find out why, with a test that closes the signal the way it closed in the live
   run, and fix it. If the cause is a gap in the design, report it instead of inventing behavior.

Acceptance criteria:
* A 409 reply to `getUpdates` produces a health message that contains `409` and Telegram's description; a body that is neither shape still says it is unreadable.
* `doctor` fails for a bot with a webhook and passes for one without; the failure names the host only.
* Table-driven header test: in a component topic, in General, in a private chat, with `get_task` failing, with a title longer than the limit, and an information message.
* The test for item 4 fails without the fix.
* The gate passes.

### C9. Readable messages (normal)

The first live messages were one block of plain text, and the recommended option was marked only in
the text. Design §6.1 now has the layout (**Layout** bullet and the example). Do not edit
`docs/design.md`. Run this after C8, which changes the header.

1. `Message` gains `heading: str = ""` (a core type; it knows nothing of Telegram). The Telegram adapter
   sends the heading in bold and the rest as plain text, with `parse_mode` HTML and every character of
   every text escaped (`&`, `<`, `>`), so a question that contains `<b>` shows as typed. The final edits
   of a message (A11c4) and `Details` use the same rendering. The 4096 limit counts the characters Telegram
   shows, not the markup.
2. The Product handler builds escalation and information messages with the header as the `heading` and a
   blank line between blocks. An option line is `★ 1. Label (consequences)` as today.
3. An answer button carries the option number, the ★ when the option is recommended, and the label,
   cut to a new setting `button_label_max_chars` (default 30, with `…`). Button data does not change. The
   buttons for `Reply with text`, `Cancel task` and the rest keep their labels.
4. Other handlers' messages (review, notice) are not restyled. If one of them needs the heading to look
   right, report it.

Acceptance criteria:
* Table-driven: an escalation with a recommended option, without one, with a long label, and an information message; each checks the heading, the blank lines and the button labels.
* A question containing `<b>` and `&` is delivered escaped, in a send and in a final edit.
* A recommended option has the ★ on its button and on its line; no other button has one.
* The gate passes.

### C10. An engine that is up again closes its signal (normal)

Found in the live run: the engine was killed twice. The first `engine_down` was restarted and
resolved. The second was forwarded (the loop guard refused a second restart within the hour), and after the
PO started the engine by hand the signal stayed `forwarded` for minutes, with its message unedited and its
reminders running. Design §6.1 says these signals close when the engine is up again, and nothing does.

Close every open `engine_down` and `engine_fault` signal of a project when the liveness check sees
`status.engine_up` true: use the existing `Closing` path with a final line ("Engine is up again") so the
message is edited, its buttons removed and its reminders stopped. A signal that a handler has parked or
is handling is left to that handler.

Same flaw, second case, found in the same run: tapping *Acknowledge* on an information message closed
its signal but left the message as it was, with the button still on it. `acknowledge_notice` in
`journal/replies.py` moves the signal to `closed` with no final line, and the final edit only runs for a
signal that has one. Close it with the final line "Acknowledged", so every copy of the message is edited
and loses its buttons. A late tap on a signal that already ended still changes nothing.

Also fix the wording of a forwarded `engine_down`, which the live run showed as: "The engine of api is
down; its last event was engine_started. What I tried: No more automatic restart within 1:00:00; already
done at 2026-10-31T18:11:51.603935+00:00.; failed checks: none". It names an internal event, shows a raw
timestamp and a duration as `1:00:00`, ends with a stray `.;`, says "failed checks: none", and does not
say what the PO should do. Make it read, for example: "The engine of api stopped unexpectedly. I restarted
it at 18:11 UTC, and I restart at most once per hour, so please start it yourself with `shikiru start`
in the api project." Rules: a time as `HH:MM UTC` (the date only when it is not today), a duration in
words ("1 hour"), no event kinds, the doctor's failed checks listed only when there are some, and one
sentence per fact. The same rule applies to the other notes that the loop guard and the restart action
write; report any you find that read badly.

Acceptance criteria:
* Table-driven over the signal states (`new`, `handling`, `parked`, `forwarded`) and kinds (`engine_down`, `engine_fault`, another kind): only a `forwarded` signal of those two kinds is closed, and only when the engine is up.
* The message of a closed signal is edited to the final line with the buttons removed, and the Scheduler sends no reminder for it afterwards.
* Acknowledging an information message edits it to "Acknowledged" with the buttons removed, in every copy; a second tap changes nothing.
* A composed test (as in C7) that forwards an `engine_down` and then reports the engine up ends with the signal closed and the message edited; it fails without the fix.
* A table-driven test of the engine note: refused restart with and without failed checks, a restart from today and from an earlier day; no raw timestamp, no `.;`, no event kind in any of them.
* The gate passes.

### C11. A message in a topic is not a reply to the topic (normal)

Found in the live run: `/help`, `/status`, `/open`, `/stats` and `/goal` sent in a topic of the group
were stored as inbound kind `reply` and got no answer. In a forum topic Telegram puts a
`reply_to_message` on every message, pointing at the topic's first message, whose `message_id` equals
the message's `message_thread_id` (journal: `reply_to_ref` = `<chat>/173` with `thread` = 173). The
parser in `adapters/telegram_updates.py` takes it for a real reply, so Replies looks for an origin message
that does not exist and does nothing. Commands never worked in a topic, and so would free-text goals.

Treat a `reply_to_message` whose `message_id` equals the message's `message_thread_id` as no reply (the
item is `text`). A reply to any other message stays a reply. Check the other places that read
`reply_to_ref` for the same assumption and report them.

Second part, from the same run: tapping *Reply with text* sends "Reply with your answer." and the PO must then
long-press it and choose Reply, or the text is not tied to the question (see above). Make the app open the
reply box by itself. `Message` gains `expects_reply: bool = False` (a core type; it knows nothing of
Telegram). The Telegram adapter sends such a message with `reply_markup` `{"force_reply": true,
"input_field_placeholder": "Your answer"}` (and no keyboard), in a topic as well. The prompt that
*Reply with text* sends, and any other prompt that waits for text, sets it. The PO's answer then arrives as
a reply to that prompt, which Replies already ties to the question.

Acceptance criteria:
* A prompt that waits for text is sent with `force_reply`; an ordinary message and a message with buttons are not, and a message cannot have both buttons and `force_reply` (the Bot API takes one `reply_markup`).
* A reply to the prompt resolves the escalation with the text (composed test as above, tap *Reply with text*, then the reply).
* Table-driven over payload fixtures in the shape Telegram sends in a forum topic: a command in a topic, a plain message in a topic, a reply to a message of the bot in a topic (stays a reply), a command in a private chat, and a command in a group without topics.
* A composed test (as in C7): `/help` sent in a component topic gets an answer in the same topic; it fails without the fix.
* The fixtures hold no real user ids or tokens: use the ids already in `tests/fixtures/telegram`.
* The gate passes.

### C12. Commands answer when they have nothing to show (normal)

Found in the live run: `/open` with no open question or approval sent nothing, so it looked broken, and
`/stats` (a CLI command only) was treated as free text and answered with the `/help` list, which did not
say the command is unknown.

1. `/open` answers "Nothing is waiting for you." when it re-shows nothing and every project answered
   (the unreachable-project line stays as it is, and is not repeated with it).
2. A message that starts with `/` and is not a command the Operator knows is answered with
   "Unknown command /stats." (the name as typed, cut to a named limit) followed by the help list. Free
   text that does not start with `/` keeps today's answer.

Acceptance criteria:
* `/open` with nothing open answers once and a repeat of the same update answers nothing more; with one open question it re-shows it and does not say "nothing".
* Table-driven: `/stats`, `/Open` (case), `/open@bot` (Telegram's addressed form, which is a known command), a long unknown name, plain text.
* The gate passes.

## Phase D: install a missing tool

Design §3.2, §5, §6.3, §8. All tasks are `normal`. D1 (the spike) is done: `tasks/reasoner-spike.md`
holds the verified flags, and the adapters follow it. Rules for the phase: a model never writes a
command, names are validated by code, every command is a stored argument list built from a fixed
recipe, nothing runs without the PO's tap, and no test calls a real agent CLI, a real package manager
or the network (fakes and `echo`/`sleep` through `CommandRunner`).

### D2. Reasoner adapters and `assess_blocker` (normal)

`adapters/reasoner_cli.py`: a Claude adapter and a Codex adapter that implement one rung, run through
`CommandRunner` with the flags of `tasks/reasoner-spike.md`, in a scratch directory under the instance
directory, with an input cap and `reasoner.timeout_s`. Extend `CommandRunner.run` with optional `cwd`
and `env` (the adapter and the Executor need them). The prompt is a fixed template that marks the
escalation text as evidence, not instruction, and lists the host facts. The answer is parsed by a
pydantic model (`verdict`, `tool`, `manager`, `package`, `why`, `suggestion`; `extra="ignore"`).
`Assessment` gains the optional `tool`, `manager`, `package` and `why` (raw, unvalidated). A rung
that cannot run, times out, exits with an error or returns invalid output raises `ReasonerError`
naming the rung and the reason.

Acceptance criteria:
* With a fake runner that returns the recorded shape of each CLI's output, both adapters return the same `Assessment`; the argument list holds the spike's flags and the schema, and never the escalation text as a flag value other than the prompt argument.
* A timeout, a non-zero exit, text that is not JSON and JSON that misses a required field each raise `ReasonerError` that names the rung.
* An escalation text that looks like an instruction (for example "ignore the schema") is passed as evidence inside the prompt template and changes no flag.
* The gate passes.

### D3. Ladder and the safe tool list (normal)

Found in the D2 review. (1) Remove `Read` from the Claude tool list in `adapters/reasoner_cli.py`: `Read`
cannot be limited to the scratch directory, and a model that can read any host file and fetch web pages
is an exfiltration path for injected escalation text. Keep the web lookups and the two `brew` queries and
`command -v`. Change design §8 to say the same (the tool list has no file reading), and keep
`tasks/reasoner-spike.md` in step. (2) Rungs take `adapter:model:effort`: parse it in the settings model
(an unknown adapter fails at start) and pass the effort to the CLI where it has a flag, else ignore it
and say so in the spike note. (3) Record usage and duration per call when the CLI reports them, in the
signal's verdict record (design §8). (4) `app/reasoner_ladder.py`: `LadderReasoner` implements `Reasoner`
over the rungs, tried in order; a failed rung is skipped for `cooldown_min`; at most `reasoner.max_parallel`
calls run at once; every rung failing raises `ReasonerError` with each rung's reason.

Acceptance criteria:
* The Claude argument list holds no `Read`; a test pins the whole tool list.
* `claude:haiku:xhigh`, a rung without effort, and an unknown adapter parse as specified.
* A first rung that fails is skipped within the cooldown and tried again after it (test-time clock); all rungs failing raises `ReasonerError` naming every rung.
* More than `max_parallel` simultaneous calls wait.
* The gate passes.

### D3b. Wiring, host facts, health and doctor (normal)

`app/host_facts.py` collects OS, which of the five managers exist and the login-shell PATH through
`CommandRunner`. In `bootstrap.py` the Reasoner, when configured, becomes the `assess` function of
`RuleClassifier` (host facts go into the prompt); it never raises: a failure becomes `Assessment(OTHER,
"Reasoner failed: …")`, which the classifier sends to `product` with that note. The health source raises
one `health` signal while the whole ladder fails and closes it on the next success. `doctor` probes that
each rung's CLI exists and `/status` shows each rung's state.

Acceptance criteria:
* All rungs failing forwards the signal to `product` with the `Reasoner failed` note and raises one `health` signal, not one per call; a later success closes it.
* Without a `[reasoner]` section nothing changes: `decision_required` goes to `product` as today.
* A `decision_required` signal with a fake Reasoner verdict `host_tool` is classified `infrastructure` once D5 registers its action, and the host facts reach the prompt.
* `doctor` fails for a missing CLI and names the rung.
* The gate passes.

### D4. Names, recipes and the existence check (normal)

`domain/recipes.py` (pure): the name pattern of design §6.3 (letters, digits, `.` `_` `-` `+`, and `@` and
`/` for npm; at most 100 characters; a name never starts with `-`), the allow-list of managers and the
recipe table `brew`, `uv`, `npm`, `pipx`, `cargo` as fixed argument lists. `validate_install(tool, manager,
package, host)` returns an `InstallPlan` (tool, manager, package, argument list) or a typed refusal with a
reason (bad name, manager not allowed, manager missing on this host, no recipe). The check `command -v
<tool>` is built from the validated name and run by an `app/tool_check.py` through the login shell, with
the tool name passed as an argument and not spliced into the command string.

Acceptance criteria:
* Table-driven: valid names pass; names with spaces, `;`, `$(…)`, quotes, newlines, a leading `-`, an empty string, 101 characters, and `apt`, `sudo`, `dnf` as manager are refused with the right reason.
* Each recipe yields the exact argument list of design §6.3; no recipe contains a shell string.
* The check returns the path found, or "missing"; a hostile tool name never reaches it.
* The gate passes.

### D5. Install approval (normal)

Split (decided): D5 is implemented as D5b to D5e, one gate-green change each. D5 stays unchecked
until D5e and every original acceptance criterion below pass. The approval payload lives with the
signal's classification in a numbered journal migration; the Install action reads it by signal id.
The host-manager set is collected by the same typed login-shell probe as the Reasoner's prompt.
Approved install ops remain approved and await D6; D5 does not execute package managers.

The **Install** action in `extensions/infrastructure` for `decision_required` with verdict `host_tool`
(the rule is added with the verdict, and the product catch-all stays last). It validates (D4), checks the
tool is missing (found: forward with the path), stores an `install` op in state `intended` with the exact
argument list, and sends the approval message with **Install** and **Decline**: the tool, the reason
marked as model-written, the command as built, the manager and the effect text of §6.3. It returns
`Notified`. The tap goes through Replies: Install moves the op `intended → approved` once (a conditional
change, so two taps approve once; a tap on an approval older than `approval_expiry_h`, or on an
escalation resolved elsewhere, is void and says so); Decline forwards the original escalation. Any
validation failure forwards with the reason and the model's suggestion.

Acceptance criteria:
* Composed test: a `decision_required` signal with a fake Reasoner verdict `host_tool` sends one approval message and no command runs.
* Two Install taps approve once; an expired approval and an approval for a signal resolved elsewhere do nothing and say why; Decline forwards the original escalation with a note.
* A hostile package name, an unknown manager and a tool that already exists each forward with the reason and run nothing.
* The stored argument list is the one shown in the message.
* From the D4 review, in `domain/recipes.py`: an npm package is either a plain name or `@scope/name` with one slash. `user/repo` (npm reads it as a GitHub repository) and a leading `/` are refused as `bad_package`; add them to the D4 table test.
* The gate passes.

#### D5b. Proposal persistence and host managers

Carry the typed proposal from the Reasoner's `Assessment` through `Classification` and
`JournalRouting`, persist it on the signal row, and expose a read port keyed by signal id. Extend the
read-only host-facts probe to return its prompt lines and typed observed-manager set from the tagged
probe output.

Acceptance criteria: proposal round-trips through the classification and journal; manager
availability is based on the host probe and prompt facts remain available to the Reasoner; gate
passes.

#### D5c1. Install op payload, journal port and callbacks

Add the typed stored install payload, the injected journal port and the journal's install-op access.
Add the numeric Install and Decline callback forms. Do not add approval transitions yet.

Acceptance criteria: in-memory SQLite proves `intended`, payload round-trip and repeated intention
preserving the first stored argv; valid callback forms decode and malformed forms are refused; gate
passes.

#### D5c2. Install preparation

Read the stored proposal and host-manager observation, validate through D4, then run the read-only
existence check. Return a validated plan or a forwarding outcome. This part creates no op and queues
no message.

Acceptance criteria: hostile and incomplete proposals, unknown or absent managers, and existing
tools forward with a reason; refused names never reach `ToolCheck`; gate passes.

#### D5c3. Persisted approval and outbox delivery

Integrate successful preparation into the Install action. Persist the stable install op before
queueing `Purpose.APPROVAL` with the signal number. Render the approval from the stored payload and
argv, including all §6.3 fields. Replays use that payload and queue one logical approval per
destination; do not offer Install for an approved or final op. No package-manager command runs.

Acceptance criteria: composed action/journal/outbox tests cover initial delivery, repeat handling
and recovery after the op is persisted before queueing; the command shown equals the stored argv;
gate passes.

#### D5d1. Conditional install decisions

Add journal transitions from `intended` to `approved`, `declined`, `expired` or `void`, with
the callback's signal number bound to the stored op. Declined, expired and void operations are
final for reconciliation. No external effect belongs in this part.

Acceptance criteria: in-memory SQLite proves each transition is conditional, final and bound to
the op's signal number; gate passes.

#### D5d2. Callback routing and origin binding

Route only Install and Decline buttons from a stored `Purpose.APPROVAL` origin through Replies.
Keep free text and other purposes ignored. Bind each tap to the stored approval key, signal and op;
carry the original model suggestion on the reply origin for a later Decline forward.

Acceptance criteria: the callback types, stored origin metadata and dispatch path are wired without
changing existing approval behavior; a focused Replies test proves button dispatch and rejects
unrelated origins; gate passes.

#### D5d3. Install decision effects

Bind Install and Decline taps to the approval's stored origin, signal and op. A valid Install tap
conditionally approves once before the original approval expiry; stale or resolved escalations are
explained and ignored. Decline invalidates the pending op before forwarding the original escalation.

Acceptance criteria: duplicate taps, expiry, resolved-elsewhere, decline and replay are covered; gate
passes.

#### D5e1. Journaled install approval replay in `/open`

Extend the journal and `/open` to re-show only pending intended install ops from their stored
payload, while the original approval lifetime still applies. Give each `/open` copy its own stable
outbox key while binding its buttons to the same op. Preserve current accept/cancel approvals.

Acceptance criteria: in-memory SQLite and `/open` tests cover restart replay of one stored op,
distinct and replayed update keys, expired and handled ops, and original payload/argv reuse; gate
passes.

#### D5e2. Production composition and end-to-end approval

Register the Install action and its read-only probes in bootstrap, and inject Install approval reply
handling. Preserve the generic host-tool rule and product catch-all. Exercise fake Reasoner through
classification, Install, Telegram approval, `/open` after restart, and a tap of that copy.

Acceptance criteria: the full fake-Reasoner composed path approves the same stored op after restart
and `/open`; no package-manager command or network is run; all D5 criteria pass; gate passes.

### D6. Executor and resolution (normal)

`app/install_executor.py`: when an install op is `approved`, claim it (`approved → issued`) and run the
stored argument list quoted into `$SHELL -lc`, through `CommandRunner` with `cwd` a scratch directory
outside every repository, `CI=1` and `NONINTERACTIVE=1`, closed stdin, `command_timeout_min`, under a
host lock (one host command at a time). Output goes to a file in the instance directory. Success means
the check of D4 passes, not exit code 0. Then `resolve_escalation(answer)` with "Installed `<tool>` with
`<manager>` (found at `<path>`). Please continue.", the op `confirmed` and the signal resolved. Failure
or a failing check marks the op `failed` and forwards the original escalation with the tail of the output.

Acceptance criteria:
* With a fake runner: a passing check after exit 0 resolves with the answer text above; exit 0 but a failing check, a non-zero exit and a timeout each forward with the output tail and mark the op `failed`.
* The command that runs is the stored list: changing the signal text or the model's fields after approval changes nothing.
* Two executors started together run one command (the host lock and the claim).
* No test runs a real package manager: the gate stays offline.
* The gate passes.

Split (decided): D6 is implemented as D6a to D6d, one gate-green change each. D6 stays unchecked
until D6d and every original acceptance criterion above pass. The claim, confirm and fail steps are
journal methods that move the op and its signal together; the host lock and the scratch and output
files are ports with file adapters; the executor is an app service that the approval tap starts in
a task of its own, so the serial inbound loop never waits for a host command. Wiring and the
composed install test come last. An install whose resolve is queued stays `issued` with its
acknowledgement; settling it belongs to D7.

Design check (decided): the design wins where this section differs. Design §10.1 confirms a resolve
only when the `escalation_resolved` event carries its command id, and the signal table (line 162) says
that event closes the open signal. So the `done` acknowledgement does not confirm the op. The executor
acknowledges the answer, sets the signal's final line to "Installed `<tool>` with `<manager>`." and
leaves the op `issued`; the event confirms the op and closes the signal (`closed`, not `resolved`).
The "confirm" and "signal resolved" wording of D6 and D6a is superseded: `confirm` is the event path,
not a run step. An event that arrives before the acknowledgement is not matched; D7 reconciles it, as
on the reply path.

Rework after review (decided): (1) The claim runs under the host lock, with the command. A claim voids
an approved op whose signal is no longer forwarded (design §6.3: an approval for an escalation resolved
elsewhere is void), so an op whose escalation closed before the claim, or while its run waited for the
lock, runs nothing. The check and the answer stay outside the lock. (2) A failure is forwarded before
its op is marked `failed`. The forward is retried in this process every `forward_retry_s` while the op
stays `issued`. A restart finds such an op `issued` and settles it by its check (D7). (3) A command
killed at its timeout keeps the output it printed (`CommandError.output`), which the output file and
the forwarded tail use. The runner writes the output to files rather than pipes: after a kill, a pipe
can keep unread output or block its reader. The runner also waits for the command's process group to
exit, so a child that outlives its leader cannot outlive the timeout or the host lock; a harmless
shell test covers this case.

#### D6a. Journal steps of a run

Add `InstallRunLog` to ports, with `InstallClaim` (the approved op, its signal and the suggestion).
Implement it in `journal/install_runs.py`: `claim` moves `approved → issued` and returns the stored
op, `acknowledge` saves Shikiru's acknowledgement with the output path and leaves the op `issued`,
`confirm` moves `issued → confirmed` and the signal `forwarded → resolved` in one transaction, and
`fail` moves `issued → failed`. Every move is conditional.

Acceptance criteria: in-memory SQLite tests show that a second claim, a claim of an intended op and a
fail of an approved op change nothing; that a claim returns the payload stored at approval even when a
replacement is offered afterwards; that confirm resolves the signal and keeps the command id; that a
queued acknowledgement leaves the op `issued`; gate passes.

#### D6b. Host lock and install files

Add the `HostLock` port (`hold()` as an async context manager) with an adapter that takes an exclusive
`flock` on `host.lock` in the parent of the instance directory and waits by polling with `Clock.sleep`,
so a waiting executor stays cancellable. Add the `InstallFiles` port: `scratch_dir()` (created outside
every repository, under the instance directory) and `save_output(op_id, text)` (a file in `output/`).
Add two settings with defaults: the lock poll interval and the output tail line count.

Acceptance criteria: two lock holders on one path never overlap (a real file lock, waited for with a
short poll); a cancelled waiter releases nothing it does not hold; the output file is written under
`output/` and the scratch directory exists; gate passes.

#### D6c. Executor

Add `InstallExecutor` in `app/install_executor.py`. `start(op_id)` runs `execute` in a task of its own
(as the Router does) and `join()` waits for them. `execute` claims the op, runs `[shell, "-lc",
shlex.join(argv)]` through `CommandRunner` under the host lock with the scratch `cwd`, `CI=1` and
`NONINTERACTIVE=1`, saves the output, runs the D4 check, then resolves with the answer text or forwards
the escalation with the output tail. Add the `InstallRunner` port (`start`) for the reply handler.

Acceptance criteria: the fake-runner tests of D6 pass, including the cases of the original list; two
executors started for one op run one command; the argument list run is the stored one; no test runs a
real package manager; gate passes.

#### D6d. Wiring and the composed install test

The approval tap calls `InstallRunner.start` after `approve_install` returns True, and does nothing
after it. Bootstrap builds the host lock, the files, the journal steps and the executor. The composed
install test is updated deliberately: after the tap the fake runner runs the install and the check, the
fake Shikiru acknowledges the resolve, and the op ends `confirmed`. The change is recorded in its
docstring and in the report.

Acceptance criteria: the composed approval path ends with one install command run and one resolve; the
D5 criteria that still apply pass; gate passes.

### D7. Interrupted installs and measures (normal)

Start-up reconcile for `install` ops: an op in `issued` is decided by its check, never repeated: check
passes means `confirmed` and resolve as in D6; check fails means the PO is asked again with a new
approval. An `approved` op never claimed is run. `stats` reports installs approved, declined and checks
failed (the counters design §15 lists), replacing the "unavailable until Phase D" labels. A composed
test runs the whole path: raise, verdict, approval, tap, install, check, resolve.

Failure forwards (decided in the D6 rework): an `issued` op whose failure was never queued, because the
process stopped during the forward retry, is decided by its check like any issued op. The usual result
is a failed check, so the PO is asked again with a new approval and the old one is void. A run that
exited non-zero but whose check still finds the tool passes this rule and is answered. D7 must decide
whether such a run goes to the PO first, since design §10 says only "an `install` by its check".

Decided in D7: such a run is answered at restart, not sent to the PO first. Design §6.3 step 5 defines
success as the check passing, and the install is not run again. An `issued` op with no acknowledgement
is decided by its check: the tool is found, so the answer is sent once and its acknowledgement saved;
the tool is missing, so the PO is asked again. A re-ask is a new op with identity
`install:<signal id>:after:<old op id>` and the old op's stored payload, so the PO approves the command
that was shown. The new approval is queued before the old op is voided: a crash between the two leaves
the new approval queued and the old op issued, and the next start asks again without a second
approval. A rejected answer at restart voids the op; the escalation stays forwarded, so the PO still
has the question.

Verification follow-up: the recurring reconcile pass checks Shikiru before starting an approved
op, so an event still awaiting local synchronization cannot authorize a stale install. Closed
answer resolutions confirm only with the matching acknowledgement command id; differing ids keep
the Shikiru resolution attribution. Transient lookup and check failures are retried on the next pass.

Split, one commit each, gate green per commit (the whole of D7 is about 900 lines):
* D7a. Ask again after a failed check: `InstallReasker` in `app/install_reask.py`, the re-ask identity
  in the reply binding, the void note.
* D7b. Start-up reconcile of approved and issued installs: the decision above, the in-flight check on
  the executor, the bootstrap job.
* D7c. Install counters: migration 16 (`ops.approved_at`, `ops.check_failed_at`, and the
  pre-ack `ops.resolved_command_id`), the marks set on approval and on a failed check, the `stats`
  labels.
* D7d. Crash-point tests, including an `escalation_resolved` event before its acknowledgement and a
  nonzero run whose restart check passes; composed path; check-after regression; D7 ticked.

Acceptance criteria:
* Restart with an op in `issued` and the tool present: resolves once, runs nothing. With the tool still missing: a new approval message, and the old one is void.
* A crash between the claim and the run, and between the run and the resolve, are both covered by tests with the clock and fake runner.
* `stats` shows the three install counters from a seeded journal.
* A resolved event that arrives before its acknowledgement confirms the install when the matching acknowledgement arrives; a nonzero run interrupted during its failure forward is decided by the passing check at restart.
* The composed path passes, and fails when the check-after step is removed.
* The gate passes.

## Later phases (outline)

* **B Topics:** tasks B1 to B4 above.
* **C Infrastructure basics and review:** tasks C1 to C6 above.
* **D Install:** Reasoner spike, interface, adapter and `assess_blocker`, ladder, validation and
  recipes, approval flow, Executor (with the `CommandRunner` adapter and its subprocess exemption in
  `pyproject.toml`), restart handling. `doctor` gets the Reasoner CLI probe (design §3.2) with the
  Reasoner adapter. The `assess` function given to
  `RuleClassifier` never raises: a Reasoner failure becomes `Assessment(OTHER, "Reasoner failed: …")`,
  which the classifier already sends to `product` with that note. `RuleClassifier` rejects two rules
  claiming one reason; relax that to (reason, verdict) pairs when a second category shares
  `decision_required`.
* **E Hardening:** a recovery path for an uncertain topic creation (today start-up refuses until the PO inspects the forum by hand), drills, contract-test script, a human-run `scripts/record_telegram.py` that records real Bot API replies with the PO's own bot (token read from the token file by the script only, never by the gate) to replace the documentation-shaped fixtures, crash-point tests, security review.
