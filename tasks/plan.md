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

Acceptance:
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

Acceptance:
* A handler that raises, one that times out and an unregistered category each end as a forward carrying the failure in the note.
* Signals run independently: a slow handler does not hold another signal.
* A parked signal is stored with its due time and the handler that parked it.

### A8c. Scheduler (high)

Dispatch due parked signals once, with a conditional claim, to the stored handler, also after a restart.
Reminders: `reminder_first_h`, then every `reminder_repeat_h`, bookkept as ops of kind `remind` with
one identity per signal and occurrence, so an occurrence is never scheduled twice. Delivery goes through
the journaled outbox; a recognizable duplicate after a lost send acknowledgement is allowed (§7).

Acceptance:
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

Acceptance:
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

Acceptance:
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

Acceptance:
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

Acceptance:
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

Acceptance:
* Tests against fixture payloads shaped as the Bot API documents them (kept under `tests/fixtures/telegram/`, each file labeled with its source): send, edit, rate limit, message too long, button data length.
* Each secret form is refused and never reaches the journal or the transport.

### A10c. Composition and the source-to-delivery test (normal)

Shared Shikiru clients per project, the Product extension and the classifier registered in
`bootstrap.py`, `routing_jobs(...)` and the sender appended to `Composition.jobs`, using A9's fallback
and A10a/A10b's outbox and Channel. An integration test with fake external ports and no live network
shows that a signal created by a source is classified, forwarded, queued in the outbox and sent.

Acceptance:
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

Acceptance:
* With `httpx.MockTransport`: a documented success reply, a 429 reply with `retry_after`, a 400 reply and a connection error each come back as the Channel expects; the token appears in the request URL only.
* No test reads a real token file; the gate makes no network call.

### A11. Telegram inbound and replies (split into A11a to A11c)

§7. One task was too large (700 to 900 lines with tests); the Leader's split was approved. Do one
letter per change. Each ends with `uv run poe check` and its own todo line; `docs/design.md` stays
unchanged. The design does not specify `answerCallbackQuery` (acknowledging a tap); no change invents it.

### A11a. Inbound transport and journal (high)

`getUpdates` on the same Bot API adapter, with typed inbound items (tap, reply, text) that keep the
sender and the message references. The owners allow-list, dedupe by update id, and a durable polling
offset saved in the same transaction as the allowed updates. Pending items are readable, so a crash
before Replies handles one loses nothing. Text that looks like a secret is not stored. Polling is not
wired until A11c.

Acceptance:
* The same update delivered twice is stored once.
* An update from a user id not in `owners` is not stored, and the offset still moves past it.
* A crash before the batch is journaled leaves the offset behind; a recorded item stays pending until handled.

### A11b. Replies (high)

Routing by the journaled originating message and its purpose (`question`, `approval`, `notice`):
`a:`, `d:` and `k:` data decoded separately, free text matched by `reply_to_message`, `needs_input`,
and the confirming tap for accept and cancel (§6.1). An approval message accepts only its own buttons.
Choices are resolved by re-reading the escalation view, with the numbering rule shared with the product
renderer through an inward-facing helper. The typed resolve payload is journaled as an `ops` row with an
update-based identity before `resolve_escalation`, and the acknowledgement and command id are saved.

Acceptance:
* The same update delivered twice issues one resolve, also after a restart.
* A tap on an approval message accepts only its own buttons; a free-text reply is matched by `reply_to_message`.
* An issued or uncertain op is left for A12; an acknowledgement alone never confirms a resolve.

### A11c. Polling, answers and composition (high)

Polling and delivery of pending items registered in `bootstrap.py` with the shared Bot API and Shikiru
clients. "Already resolved: … at …" from the journal for a later answer; journaled edits that remove
the buttons from every copy once a resolution is observed. A12 keeps command-event correlation, start-up
reconciliation, lost commands and resolutions made elsewhere. `/status`, `/open`, `/help`, free-text help
and `/goal` stay with A13 and A14.

Acceptance:
* An offline composition test runs an inbound tap through Replies to a journaled resolve.
* A later answer to a resolved question gets the recorded resolution and time.

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
* **E Hardening:** drills, contract-test script, a human-run `scripts/record_telegram.py` that records real Bot API replies with the PO's own bot (token read from the token file by the script only, never by the gate) to replace the documentation-shaped fixtures, crash-point tests, security review.
