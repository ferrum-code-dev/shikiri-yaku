# Shikiri-yaku: design

Status: current design. No code exists yet. It builds on what [Shikiru](https://github.com/ferrum-code-dev/shikiru) provides today: its `README.md`, `docs/design.md` and the MCP server. The implementation plan and the assessment are in [plan.md](plan.md).

## 1. Idea

Shikiru runs the development: one engine per repository, with Leader and Implementer agents, review and integration. When it cannot continue on its own it raises an **escalation** and waits for a person. Today that person watches tmux.

Shikiri-yaku (the **Operator**) takes that seat. It watches the escalations and a few other signals of every Shikiru project of a product, sorts each one into a **category**, and hands it to the **handler** for that category. A handler either resolves the escalation itself or forwards it to the Product Owner (PO) through a chat channel (Telegram first). The PO answers with a button or a short reply, and the Operator applies the answer in Shikiru.

The first release has four handlers:

* **Product**: forwards the escalation to the PO.
* **Infrastructure**: retries, waits for quota resets, restarts engines and, after the PO approves the exact command, installs a missing tool. Whatever it cannot fix it forwards.
* **Review**: sends the PO the link of a pull request that waits for review (Shikiru's pull-request integration mode).
* **Notice**: tells the PO that a task is done, or that the Operator itself has a problem.

Everything else the Operator might do later (routine decisions, goal intake, digests, more repair actions) is added as a rule, handler, action, Reasoner job or channel. The core does not change ([§13](#13-extensions)).

The Operator exists to remove idle time: work waiting for a person who is not watching, and problems a person would have fixed with one command. Its success is measured by PO wait time and by the share of signals handled without the PO ([§15](#15-proving-it-works)).

## 2. Principles

1. **Shikiru is the source of truth.** Tasks, escalations, their state and history live in Shikiru and are read and changed through its MCP server only (plus the `start` and `doctor` commands of its CLI). The Operator keeps no copy of the backlog and never touches Shikiru's database.
2. **A small fixed core, growth by extension.** Watching, sorting, routing, replying and journaling are the core. Categories, handlers, actions, Reasoner jobs and channels are extensions, wired in one place.
3. **Every route ends at the PO.** An unknown signal, an unsure classification, a failed or exhausted handler, or a model that is down all mean *forward to the PO*. Nothing is dropped, and a new extension can never do worse than plain forwarding.
4. **Agents propose, code acts.** The Operator's reasoning agent may look things up with read-only tools but cannot change anything, and returns typed data. Code validates it, builds every command from it, and runs only what the PO has approved. A model never writes a shell command.
5. **Journal before acting, check after.** Every effect (a Shikiru command, a host command, a message) is recorded before it happens, and success is checked by observation, not assumed.
6. **No model is needed to stay useful.** Watching, rule-based sorting, forwarding and replies are plain code. With every model down the Operator still forwards everything.
7. **Nothing of the Operator is stored in the managed repositories,** just as Shikiru keeps its own state out of them.
8. **If the Operator needs something Shikiru does not offer, add it to Shikiru** instead of working around it in the Operator. If the gap is rare and does little harm, accept it and write down what would make us fix it: a *candidate* with a trigger ([§11](#11-shikiru-what-is-used-what-is-needed)). The structured escalation view (S1) is the first case; the `submit_task` key is the second.
9. **KISS and YAGNI.** Anything not needed by the first release is listed as an extension with the trigger that brings it back, not built.

## 3. Architecture

```mermaid
flowchart LR
  SH1[Shikiru: api] & SH2[Shikiru: web] -->|events, status| SRC[Signal sources<br/>events, liveness, health]
  SRC --> CL[Classifier]
  CL -.->|decision_required| RS[Reasoner<br/>model ladder]
  CL --> R[Router]
  SCH[Scheduler<br/>parked signals, reminders] --> R
  R --> IH[Infrastructure handler]
  R --> PH[Product handler]
  R --> RH[Review handler]
  R --> NH[Notice handler]
  R -.->|failure: forward| PH
  IH --> IA[Install action<br/>host commands]
  IH & NH -->|resolve retry, start, read| SH1 & SH2
  IH & PH & RH & NH --> CH[Channel: Telegram]
  CH <--> PO([Product Owner])
  CH -->|taps, replies, commands| RP[Replies]
  RP -->|resolve, submit| SH1 & SH2
  RP -->|approve install| IA
```

Every module reads and writes the **Journal** ([§9](#9-journal)); it is left out of the picture. The Reasoner is used only by the Classifier: its verdict is stored with the signal, and the Install action reads it from there.

One Python process with one asyncio loop, one SQLite journal, one TOML config and the OS service manager. Modules, not services:

| Module | Responsibility | Uses a model? |
|---|---|---|
| **Signal sources** | An event source (one supervised `shikiru --project P mcp` link per project, durable cursor), a liveness source (engine up or down) and a health source (the Operator's own links and Reasoner). All only produce signals ([§4](#4-signals)) | No |
| **Classifier** | An interface that assigns a category to a signal. The first implementation is an ordered rule table; for `decision_required` it asks the Reasoner ([§5](#5-classification)) | Only for `decision_required` |
| **Router** | Finds the handler for a category and turns every failure into a forward | No |
| **Scheduler** | Wakes parked signals and sends due reminders | No |
| **Loop guard** | Limits automatic actions per cause, from the journal ([§6](#6-handlers)) | No |
| **Handlers** | Product, Infrastructure, Review and Notice ([§6](#6-handlers)) | Infrastructure: through the Reasoner's verdict |
| **Reasoner** | One job, `assess_blocker`, behind an interface; adapters that call agent CLIs with read-only tools, tried in a ladder ([§8](#8-reasoner)) | Yes |
| **Install action** | Host facts, command building, approval, running, checking ([§6.3](#63-install-a-missing-tool)) | No |
| **Channel** | Interface plus the Telegram adapter: outbox, inbound dedupe, sender allow-list ([§7](#7-the-po-channel)) | No |
| **Replies** | Looks up the purpose of the message an inbound item belongs to (`question`, `approval`) and maps button taps, replies and commands to Shikiru commands or to the install action | No |
| **Journal** | SQLite store ([§9](#9-journal)) | No |

**Extensions.** A category, its classification rules, its handler and the handler's actions form one **extension**: one folder of code, registered in one place. The first release registers four: product, infrastructure, review, notice. Rules and handler of an extension are never spread over the core ([§12](#12-engineering-rules)).

**Why a separate service, not a plugin in an agent session.** Watching, pending questions and message delivery must keep working when an agent runs out of quota, changes or crashes.

One Operator instance serves one product, with its own instance directory, `journal.db`, chat bot and service unit. There is no registry of products; several products mean several instances. The layout, the configuration, the command line and the service setup are documented in the [README](../README.md) and are not repeated here.

## 4. Signals

A **signal** is one thing that may need attention. All signals have the same shape, so classification and routing do not care where they came from: a stable `id`, the `project`, the `task_id` and `escalation_id` when known, a `kind` (`escalation`, `engine_down`, `engine_fault`, `task_done`, `pr_review` or `health`), the `reason` (Shikiru's escalation reason, or the stop reason of an engine) and a `text` for the message.

**Identity.** The id has the pattern `<project>:<kind>:<source id>`, where the source id comes from the escalation, the event or, for `health`, the failure and its start. It is the same after a restart, so duplicates are impossible.

**Event source.** One reader per project long-polls Shikiru's events. An escalation, a finished task, a pull request ready for review and an engine stopped by a fault each raise a signal. A pull request's signal closes when it is withdrawn, when its task is done (merged) or when Shikiru escalates `pr_closed`. `escalation_resolved` closes the signal of that escalation, whoever resolved it ([§10](#10-restarts-and-failures)). A `task_cancelled` creates no signal; it closes the open signals of its task. Lifecycle events only update the project's state. Shikiru retries engine faults itself, so only a fault stop raises a signal. The cursor and the journal rows it causes commit in one transaction, so a crash replays at most one batch.

**First sync.** A project's history is read once, and only its current state is kept: its lifecycle, the escalations still open and the pull requests still waiting. History is never replayed as signals.

**Liveness.** A crashed engine raises no escalation, so the liveness source checks `status.engine_up` every minute. An engine that is down after it was started raises `engine_down`; one that was never started is only shown in `/status`. A clean stop records no reason, so a deliberate stop and a host shutdown look alike, and the Operator tells them apart by time. A stop observed while the Operator runs is **respected** once the engine stays down for `stop_grace_min`: it is saved and never triggers a restart, also after the Operator restarts. A clean stop that happened before this run and was never respected is treated as `engine_down` at once. So no engine is restarted against the PO's will, and none is left down after a reboot.

**Health.** The Operator's own problems are signals too: a project's MCP link that stays unreachable for `link_unreachable_min`, and the Reasoner when its whole ladder fails ([§8](#8-reasoner)). A `health` signal opens once on the failure, closes on recovery, and is not repeated while it stays open.

PO messages are not signals. They go through Replies ([§7](#7-the-po-channel)).

## 5. Classification

A **category** names who should deal with a signal. Categories are an open set; the first release has four:

| Category | Meaning | Handler |
|---|---|---|
| `product` | Needs the PO: a product choice, a delivery decision, or anything unsure | Product |
| `infrastructure` | A problem of the machine, the agents or the workflow that code may fix | Infrastructure |
| `review` | Work waits for the PO's review | Review |
| `notice` | Information, nothing to decide | Notice |

**The Classifier is an interface:** `classify(signal) → Classification(category, source, note)`. The first implementation, `RuleClassifier`, applies the rules that the registered extensions contribute. A decision model or a general LLM can replace or precede it later by changing only the composition root; the categories it may choose from come from the registered extensions.

**Rules.** Each extension contributes its own rules, and the classifier checks them in registration order; the first match wins. The product extension registers last, and its catch-all rule is the fallback. Startup fails if two extensions claim the same reason. Every signal except `decision_required` is sorted by its typed `reason` or `kind`, at no token cost, so a reason Shikiru adds later falls to the catch-all. In short: agent, integration, quota and engine problems go to infrastructure; a pull request ready for review goes to review; finished work and health go to notice; everything else, including reasons that have no action yet, goes to product.

**`decision_required`.** Without a configured Reasoner this is always `product`, so the first phases forward it at once. With one, the question waits for the verdict at most `reasoner.timeout_s`. Before the verdict, code collects host facts (OS, which package managers exist, PATH). The Reasoner's `assess_blocker` job ([§8](#8-reasoner)) reads the reason, the question, the agent's options and these facts. Its verdict is `host_tool` (a program is missing on the machine), `project_dependency` (a library the project itself must declare) or `other`. On a model failure, a timeout, an invalid answer or any other verdict the category is `product`, and the model's one-line suggestion becomes a note on the message, marked as model-written; when the Reasoner failed, the note says so. A `project_dependency` is never installed on the host: it would work here and fail everywhere else, and the fix is a change to the project's dependency file, which is the agent's job. The PO sees the suggestion "add it to `<file>`". The Operator never writes into a repository. A wrong verdict is cheap by design: the infrastructure path forwards whatever it cannot fix, and it never acts on the host without the PO's approval. Verdicts are logged with the signal so misroutes can be reviewed by hand ([§15](#15-proving-it-works)). If they prove unreliable, a typed category set by the asking agent becomes a Shikiru requirement.

**Extending.** A new category is an extension: its rules (or a new Reasoner verdict), a handler and its actions, registered in the composition root. For example `access` routed to a technical contact, or `spend` for metered usage.

## 6. Handlers

A handler takes one signal and returns one **outcome**:

```python
class Handler(Protocol):
    async def handle(self, signal: Signal) -> Outcome: ...

Outcome = Resolved(note) | Forward(note) | WaitUntil(time, note) | Notified(note)
```

* `Resolved`: the handler applied a fix in Shikiru; the signal closes.
* `Forward`: the signal goes to the product handler, with the note saying what was tried and why it stopped.
* `Notified`: the handler sent the PO its own message about the signal (the Review handler's pull-request message). The signal stays open and unanswered like a forward, so reminders apply, but the Product message is not sent.
* `WaitUntil`: the signal is parked with a wake-up time and dispatched to the same handler again then. The handler records what it is waiting for as an op of kind `wait`, so on the second call it finds that op due and performs the follow-up action instead of waiting again.

Each handler gets what it needs through its constructor (the Channel, the journal and its actions), so its dependencies are visible and fakeable ([§12](#12-engineering-rules)). A handler that does several things, as the Infrastructure handler does, receives them as **actions**: small objects `Action.run(signal) → Outcome`, each built with its own dependencies (the Shikiru client, the install action, the loop guard) and registered with the reasons it covers. A new action is added without editing the handler, and tests use fake actions. The handler registry is a plain dictionary built in the composition root.

**The Router guarantees the fallback.** An exception, a timeout or an unknown category becomes `Forward`. Signals are handled as independent asyncio tasks, and the only shared resources are the host lock and the Reasoner (at most `reasoner.max_parallel` calls at once).

**Loop guard.** An automatic action is recorded in `ops` with its `(project, task, reason, action)`, and a handler asks the guard before acting. The budgets are settings ([configuration reference](../README.md#configuration-reference)). A spent budget becomes `Forward` with the history, so a fix that does not hold never loops.

### 6.1 Product handler

It forwards. The message is a template over Shikiru's fields, so it needs no model. The question and the agent's options, with their consequences and the recommended one, come from the structured escalation view ([S1](#11-shikiru-what-is-used-what-is-needed)). An escalation without agent options offers Shikiru's own resolutions (`retry`, `accept`, `cancel`), each only where the escalation allows it.

* **Answers.** An option resolves the escalation with `answer`, with the option's label and consequences as text. An option that needs input asks for a reply first, and a free-text reply to the message is sent as the answer. `accept` and `cancel` ask for a confirming tap.
* **Information.** A signal without an escalation is shown as information. A message that only informs closes when it is sent. A message about something that stays true until it is fixed carries *Stop reminders*, which silences its reminders and leaves the signal open.
* **Silence never approves anything.**

### 6.2 Infrastructure handler

The handler picks an **action** by the signal's reason or kind, from the registered actions:

* **Retry**: `agent_error`, `agent_stuck`, `agent_protocol`, `integration_conflict` and `integration_blocked` are given back to Shikiru with `resolve_escalation(retry)` at once; Shikiru has already backed off before escalating. The loop guard forwards a second escalation of the same cause.
* **Wait**: `quota_exhausted` and `ladder_exhausted` are parked until the latest reset of the windows that are used up, plus a margin, and then retried. A signal with no known reset, or one beyond `quota_wait_max_h`, is forwarded.
* **Restart**: `engine_down` and `engine_fault` start the engine with `shikiru start` and confirm `status.engine_up`; Shikiru resumes its tasks itself. A failed or repeated start is forwarded with the failed checks of `shikiru doctor`. `engine_error` gets the same restart if the engine is down, and then its escalation is retried.
* **Install**: `decision_required` with the verdict `host_tool` goes to the install action ([§6.3](#63-install-a-missing-tool)); any failure is forwarded.

Shikiru already waits for quota resets of up to 8 hours on its own, so a `quota_exhausted` escalation means the reset is further away or unknown. The Operator uses `resolve_escalation(retry)`, never `retry_task`. The first is rejected by Shikiru if the escalation is already resolved; the second interrupts and requeues whatever the task is doing, so repeating it after a restart would be harmful. Product code is never repaired by the Operator; retrying gives the work back to Shikiru's agents.

### 6.3 Install a missing tool

The Operator runs as the PO's user, so a command the PO approved is the same as one the PO ran. The Reasoner only points at *what* is missing. **Code builds the command.**

```
decision_required, verdict host_tool
  → validate        code: tool, manager and package fields
  → check before    code: command -v <tool>
  → approval        PO taps Install or Decline
  → run             code, under the host lock
  → check after     code: command -v <tool>
  → resolve         answer "Installed <tool> ..." to the Leader
```

1. **Validate.** The Reasoner's `tool`, `manager` and `package` must match strict name patterns, the manager must be on an allow-list and exist on the host, and the pair must have a recipe. Anything else is forwarded. A recipe is a fixed argument list, never a shell string; the supported managers are in the [README](../README.md#install-approval). A system package manager or `sudo` is never run, so the signal is forwarded with the suggestion instead.
2. **Is it really missing?** Code runs `command -v <tool>` in the user's login shell. If the tool is found, nothing is installed: the agent could not see a tool that exists, so the signal is forwarded with the path found.
3. **The PO approves the exact command.** The message shows the tool, the model's reason (marked as model-written), the command as code built it and its effect. The argument list is stored before the message is sent, and what runs is that stored list, never re-derived. **Decline** forwards the original escalation. The approval is a conditional state change (`intended → approved`), so two taps cannot start two installs, and it is void after 24 hours or once the escalation is resolved elsewhere.
4. **It runs like the PO would run it:** in the login shell, outside every repository, under the host lock, with a timeout after which the whole process group is killed. A password prompt fails fast.
5. **Success means the check passes,** not exit code 0. The output goes to a file in the instance directory, and on failure its tail goes to the PO with the forward.
6. **Resolve.** `resolve_escalation(answer)` tells the Leader that the tool is installed and where it is. Only `answer` and `cancel` exist for `decision_required`, so a retry is not offered.
7. **Journaled before it runs.** An issued install is never repeated blindly: after a restart the check decides, and if the tool is still missing the PO is asked again.

A model writing the command itself is the "free-form approved commands" extension ([§13](#13-extensions)).

### 6.4 Review handler

Shikiru's pull-request integration mode opens a pull request for a finished task and keeps the task *integrating* until the PR is merged or closed. Nobody is asked, so there is no escalation; the PO simply has to look. The handler sends one message with the task title, the commit count, an *Open PR* link button and *Stop reminders*. The Operator does not merge, comment or review.

* **Closing.** The signal closes when the task is done (merged), when the PR is withdrawn, or when Shikiru escalates `pr_closed`; the message is then edited to its final state. *Stop reminders* only silences the reminders.
* **Reminders** follow the Product handler's. A PR left unmerged beyond Shikiru's wait budget raises `integration_blocked`; the retry of the Infrastructure handler then just starts another wait, spent once by the loop guard, and the review message and its reminders continue.
* **Later:** a review by an agent or by the CI's review agents is another handler for the `review` category, which forwards only when asked ([§13](#13-extensions)).

### 6.5 Notice handler

**Task finished.** A plan task spawns child tasks, so a message per `task_done` would be noise. The handler reads the task list, follows `parent_id` to the family root, and stays silent while any task of that family is `queued`, `running`, `integrating` or `escalated`. When none is left it sends one message per family root, saying the root is *integrated* with its number of tasks. Its key is recorded in `messages`, so the message is never sent twice. Shikiru records a task as done only after integration settles, so release is not tracked.

**Health.** A `health` signal becomes one message, edited to say that it has recovered when the signal closes.

The infrastructure handler sends its own messages when it acts (a quota wait and its time, an engine restart) through the same Channel. A daily digest is a later extension.

## 7. The PO channel

**Channel interface.** `send(destination, message) → message_ref`, `edit(message_ref, message)`, and a stream of inbound items (button tap, reply, text, command), each with the sender. A **destination** says where a signal's message goes; the adapter decides it from the signal's project and kind. An adapter implements the interface for one platform; the first release has Telegram only, and the Router and handlers know only the interface.

**Telegram adapter.** One bot per instance, long polling, inline buttons, edit in place. Updates are saved before the offset advances. Only allow-listed user ids are served. Without `channels.telegram.group_id` the destination is the private chat of each owner; with it, the destinations are the group's forum topics (below). Handlers see topics only through the Destinations port: a signal's destinations, and the component a thread belongs to.

* **Size limits.** A long question or option list is cut to fit Telegram, with a *Details* button that sends the full packet in parts. Button data is small, so it carries numbers only: the signal number (shown as `S41`, the journal row id) and the choice. A tap is matched to the journal by these numbers.
* **Answers apply once.** Replies are matched by the message they reply to; an approval message accepts only its buttons. Shikiru rejects a resolution for an escalation that is no longer open, so "first answer wins" is Shikiru's guarantee; the Operator tells the later answer "Already resolved: B at 12:03" from its own journal. After resolving, every copy of the message is edited to its final state and its buttons are removed ([§10.1](#101-commands-to-shikiru)).

**Forum topics.** With `channels.telegram.group_id` set, the group is a forum and signals go to its topics. One topic per component carries every signal of that component; the built-in General topic carries the Operator's own health, and a component's signals never go to General. Start-up creates the missing topics and closes those of removed components, deciding from its own journal alone, because the Bot API cannot list a forum's topics. A topic made by hand is not adopted automatically, and an uncertain topic creation stops start-up until `telegram adopt` or `telegram forget` repairs it. Only owners are served, whatever other members the group holds.

**Commands.** `/status`, `/open`, `/goal` and `/help` are documented in the [README](../README.md#chat-commands). `/goal` is journaled before it is sent, identified by the Telegram update id, so a duplicate delivery cannot submit twice. After a crash, a goal whose acknowledgement was not recorded is never resubmitted; the PO is asked to check first.

**Secrets never travel through chat.** A message that looks like a secret is refused and, where the platform allows, deleted.

## 8. Reasoner

A thin module: an interface, one job, and adapters tried in a ladder.

| Job | Input | Output (schema-checked) |
|---|---|---|
| `assess_blocker` | Escalation reason, question and agent options; host facts | `verdict` (`host_tool`, `project_dependency`, `other`), `tool`, `manager`, `package`, `why`, `suggestion` |

* **Adapters.** An adapter runs one agent CLI headless with an input cap, a timeout and a JSON schema for the answer, in a scratch directory so no repository instruction file is loaded. Claude and Codex are two adapters; another agent is another adapter.
* **Ladder.** `reasoner.ladder` lists rungs written like Shikiru's agent specs, `adapter:model:effort`, tried in order. Any failure of a rung (quota, login, timeout, invalid output) moves to the next, and a rung that failed is skipped for `cooldown_min`, so a dead rung does not add its timeout to every question. The ladder is a wrapper behind the same interface, kept in memory, with no quota accounting of its own. When every rung fails the result is `Forward`.
* **Limited tools, not none.** The job may look things up so that its answer rests on the current state of the world and not on the model's memory: web search and documentation fetch, and read-only package queries (`brew info`, `brew search`, `command -v`). It may not read files, because file reading cannot be limited to the scratch directory and host-file access together with web fetch would let injected escalation text leak data. It may not write files or run anything else. The tool limit is defence in depth. The guarantee that matters does not depend on the model: code validates every name and builds the command, and the PO approves it. The residual risk is that injected escalation text steers a web fetch; the same text already goes to Telegram. The first task of the Reasoner phase is a spike that confirms the flags for "tools limited" and "schema-checked answer" on the installed Claude and Codex CLIs. An adapter whose CLI cannot restrict tools runs with none.
* **Untrusted text in, data out.** Escalation and log text is evidence, never an instruction. The output is validated, every name is checked by code, and the model never supplies a command.
* **Failures are visible.** A forwarded message says when the Reasoner failed and why. When the whole ladder fails, the health source raises one `health` signal, closed on the next success, so the PO is told once that decisions arrive without a suggestion, not on every call. `/status` and `doctor` show the state of each rung.
* **Usage and timing** are logged per call in the signal's verdict record when the CLI reports them. They are not part of Shikiru's usage ledger; the calls are few and cheap.

New jobs (writing briefs, reading free text, summaries) are added here with their schemas. A second infrastructure kind is the trigger to split `assess_blocker` into classification and preparation.

## 9. Journal

One SQLite file (WAL), written only by the Operator. Its contents are small, and most of it can be rebuilt from Shikiru.

| Table | Contents | Why it must survive a restart |
|---|---|---|
| `cursors` | Per project: last event id, last lifecycle event, and the id of the last respected clean stop | Resume without losing or replaying events; the liveness rule |
| `signals` | Id and number, project, kind, reason, text, category and who set it (rule or Reasoner, with its verdict and timing), handler, state (`new → handling → parked \| forwarded → answered → resolved`, or `closed`), notes, wake-up time | Deferred work, no double handling, misroute review |
| `messages` | Signal, channel, chat, platform message id, purpose (`question`, `approval`, `notice`; review and health messages are notices), a unique key for notices (`done:<project>:<root task>`), delivery state; inbound items for dedupe | Edits, reminders, duplicates recognized |
| `ops` | Every effect: kind (`resolve`, `start`, `submit`, `install`, `wait`), identity or Telegram update id, project, task, reason, payload (for an install: the exact argument list), state `intended → [approved] → issued → confirmed \| failed \| unknown`, Shikiru's command id, output path, timestamps | Safe restart, the loop guard, approvals that run exactly what was shown |

The schema is created and changed by numbered migrations applied at start, as in Shikiru. Logs go to the service manager (stderr); the journal is the audit trail.

Not stored: tasks, escalation packets, logs, agent usage. They stay in Shikiru and are read on demand. There is no retention job and no scheduled backup in the first release; the file is small, so copying it is enough ([§13](#13-extensions)).

## 10. Restarts and failures

On start the Operator takes `instance.lock` (see the [README](../README.md#diagnostics-and-recovery)), then:

1. **Ops.** Every op that is not final is reconciled by observation ([§10.1](#101-commands-to-shikiru)): a `resolve` by the escalation's state and events, a `start` by `status.engine_up`, an `install` by its check, a `submit` by asking the PO.
2. **Signals.** Signals that were `new` or `handling` when the Operator stopped are handled again: a handler may run twice for one signal, so its effects are keyed by the signal (an op identity, a message per signal). Parked signals whose wake-up time has passed are dispatched. Open forwarded messages are re-sent only if their delivery was not confirmed, carrying their stable reference (`S41`), so a duplicate is recognizable.
3. **Sources.** The liveness rule of [§4](#4-signals) runs at once.

**Other failures.** A broken MCP link is restarted with backoff, and the PO is told through a `health` signal if a project stays unreachable for `link_unreachable_min`. With the Reasoner down everything that needs it is forwarded, and one `health` signal says so ([§8](#8-reasoner)). The service manager restarts the Operator.

### 10.1 Commands to Shikiru

Shikiru queues every command durably and answers `done`, `rejected` or `queued` (the engine is down, so the command waits). There is no tool to look up a command's result later, so the Operator confirms by observing:

| Command | Confirmed when | Otherwise |
|---|---|---|
| `resolve_escalation` | The `escalation_resolved` event carries the command id from the acknowledgement | `rejected` is shown to the PO with Shikiru's message ("already resolved", "resolution not valid for this reason"). A `queued` command whose escalation is still open `lost_command_min` (default 2) minutes after the engine is up again counts as lost, and the question is shown again |
| `start` | `status.engine_up` is true | Counted against the restart budget |
| `submit_task` | The acknowledgement, which carries the task id | Uncertain after a crash: ask the PO ([§7](#7-the-po-channel)) |

**Resolved elsewhere.** The PO may answer in tmux or the dashboard. An `escalation_resolved` event for an open signal that the Operator did not cause closes it, and every copy of its message is edited to "Resolved in Shikiru: *resolution*".

## 11. Shikiru: what is used, what is needed

**Used as it is** (verified in Shikiru's code):

| Capability | Used for |
|---|---|
| `wait_for_events`, `status` (`engine_up`, quotas with `resets_at`), `list_escalations`, `get_escalation` (the escalation, its packet text and allowed resolutions), `list_tasks`, `get_task` | Signals, messages, `/status` |
| `resolve_escalation` with `answer`, `retry`, `accept`, `cancel`; rejection when the escalation is no longer open or the resolution is not valid for its reason; the command id in the `escalation_resolved` event | Applying answers exactly once, and confirming them |
| `submit_task` | `/goal` |
| `engine_stopped` carries a stop reason only on a fault stop; `pr_opened` (with `url`) and `pr_withdrawn` events; a task stays *integrating* until its PR settles | The liveness rule; the review handler |
| CLI `shikiru --project <path> start`, `doctor --json` | Engine restart and diagnostics |

**Required.**

| # | Requirement | Why |
|---|---|---|
| **S1** | **A structured escalation view.** `get_escalation` and `list_escalations` also return `question`, `agent_options` (each with `label`, `consequences`, `recommended`, `needs_input`) and `resolutions` (each with `kind`, `consequences`, `recommended`), taken from the packet by Shikiru's own parser, which the dashboard already uses. The `packet` text stays for the *Details* button | Without it the Operator would parse Shikiru's markdown packet format, which breaks silently when the format changes. Small and read-only: no engine, store or migration change |

**Candidates, each added only when its trigger appears:**

| Candidate | Trigger |
|---|---|
| An idempotency `key` on `submit_task` | A duplicate task from a crash, or goal intake that submits tasks without a human |
| A typed `category` set by the asking agent | Reasoner verdicts prove unreliable |
| An escalation summary in product language | The PO finds raw questions hard to answer |
| The newest event id in `status` | The first sync of a long-running project is too slow |
| Filtering by task family | Goal tracking across components is built |
| A `contract_version` in `status` | The first incompatible Shikiru change |
| The cause of a stop in `engine_stopped` (user command, signal, host shutdown) | The grace-time rule for clean stops ([§4](#4-signals)) restarts or respects the wrong engines in practice |

## 12. Engineering rules

The Operator is built by Shikiru's agents, so these rules go into the repository's `AGENTS.md` from the first commit, and the quality gate (the same checks as Shikiru's `poe check`) enforces what it can.

**Structure.**

```
shikiri-yaku/
  src/shikiri_yaku/               # the importable package name needs underscores
    domain/       signals, categories, outcomes, rule and extension types   (pure, no I/O)
    app/          router, loop guard, scheduler, replies, install action, reasoner jobs
    extensions/   product/ infrastructure/ review/ notice/                  (each: rules, handler, actions)
    ports.py      Protocols: ShikiruClient, Channel, Reasoner, Classifier, CommandRunner, Clock
    adapters/     shikiru_mcp, telegram, agent_cli, subprocess runner
    journal/      SQLite access; the only place with SQL
    bootstrap.py  the composition root: the only place that builds concrete objects
    cli.py        run, doctor, install-service
  tests/
```

**Rules.**

* Dependencies point inward: `domain` imports nothing from the other packages, `app` and `extensions` import `domain` and `ports`, adapters implement ports, and only `bootstrap.py` knows concrete classes.
* One reason to change per class. The Router dispatches; the loop guard counts; the scheduler wakes parked signals; each source produces signals. A handler does not know another handler, only the Product handler via `Forward`.
* Dependencies come in through constructors. No module-level state, no service locator, no handler receiving a bag of everything.
* Time comes from `Clock`, subprocesses from `CommandRunner`, Shikiru from `ShikiruClient`, so each is replaceable and testable. The journal is concrete SQLite (tests use an in-memory database, not a mock).
* Open for extension, closed for edits: a category, handler, action, source, Reasoner job, Classifier or channel is added by writing it and registering it in `bootstrap.py`. An extension owns its rules, so the Router, the classifier and the journal do not switch on a specific category or kind.
* Failures cross a port as typed exceptions. Only the Router turns them into `Forward`; no other code swallows them.
* Names come from this document's vocabulary (signal, category, handler, outcome). Functions are short, comments explain why, and no code exists for a case this document does not describe.
* Numbers are settings with defaults in one settings object (see the [configuration reference](../README.md#configuration-reference)), not literals in handlers.

**Tests that earn their place.**

| Test | What it protects |
|---|---|
| Table-driven classifier test with **every escalation reason Shikiru has** (the upgrade smoke script compares the list with the pinned Shikiru) plus an unknown reason falling to `product`; startup fails when two extensions claim one reason | A new Shikiru reason is not silently mishandled |
| Table-driven liveness test: engine up or down against every last-lifecycle case, including a clean stop respected after the grace time and one never respected (reboot) | No engine is restarted against the PO's will, none is left down after a reboot |
| Reasoner ladder test with fake rungs: first fails, second answers; all fail; a failed rung is skipped during its cooldown | The fallback and the health signal |
| Handler tests with fakes for each outcome, including the loop guard and the Router fallback | The "every route ends at the PO" guarantee |
| Install action tests: name validation (including hostile names), recipe building, check-before, decline, failure, interrupted run | Principle 4 |
| **Crash-point tests** of the journal: kill after queueing a resolve, after an approval, mid-command, mid-send, and check the restart result | Safe restart |
| Contract tests against a real Shikiru engine with its fake agent, run by a human-launched script: events, escalations, S1 fields, `resolve` rejection, command ids in events, `start` | The assumptions about Shikiru ([§11](#11-shikiru-what-is-used-what-is-needed)) |
| Executor tests with real harmless commands: timeout, closed stdin, killing the process group | The host-command path |
| Telegram adapter tests against fixture payloads shaped as the Bot API documents them (replaced by real recordings by the human-run script of Phase E): size limits, callback data, offset handling | The channel edge |

Not worth writing: tests of message wording or snapshots, getters, config defaults, and mocks of the Operator's own internals. A coverage percentage is not a goal.

## 13. Extensions

Each item below is an extension, a handler, an action, a Reasoner job, an adapter (channel, classifier or command runner), a source or a Shikiru candidate. None changes the core.

| Extension | Kind | What it adds | Trigger |
|---|---|---|---|
| Autonomy setting | Wrapper around handlers | Approve allow-listed installs or take cheap reversible product choices without asking, announced with an undo; promoted after repeated unchanged approvals, demoted at once on a veto | The PO approves the same kind of thing repeatedly |
| Product charter | Product handler + Reasoner job | Questions the PO's written principles already answer are answered from them | Repeated questions of the same kind |
| Goal intake | Reasoner job + handler | Free-text goals turned into briefs per component, routed by a short description of each component, with cross-component order and a submit key | `/goal <component>` proves too manual |
| Plain-language questions | Reasoner job, or a Shikiru candidate | Escalation text rewritten for a non-technical PO | The PO finds raw questions hard to answer |
| Suggested-answer buttons | Product handler | A Reasoner suggestion such as "add it to `pyproject.toml`" becomes a button | The same suggestion is typed repeatedly |
| Digest and acceptance note | Notice handler | One message a day; a note on finished work against its acceptance criteria | Notices become noise |
| Free-form approved commands | Install action | Other host commands, written by the Reasoner, shown with risk flags, approved word for word; needed for system package managers and unusual hosts | Forwarded infrastructure signals of one kind recur |
| Telegram topics | Channel adapter (destination function + startup reconcile) | A product group: one forum topic per component for all of its signals, the built-in General topic for the Operator's own health, the bot as admin with the manage-topics right; a new component gets a topic, a removed one has its topic closed with the history kept; `/goal` in a topic implies the component. By default the group must hold only the owners and the bot, because everyone in it sees project data; `allow_other_members = true` accepts other members, who can read the messages but whose taps and messages are ignored, because only `owners` are served. About 3 tasks | Built (see [§7](#7-the-po-channel)) |
| Remote components | Command runner + MCP adapter | A component with `host`: its Shikiru link, `start` and host commands run through ssh, installs run on that host with its own facts and lock | The first project on another machine |
| Review delegation | Handler for category `review` | A pull request goes to a review agent or the CI's review agents; the PO is asked only when they ask or disagree | Reviewing pull requests is the PO's main wait |
| Model classifier | Classifier adapter | A decision model or a general LLM replaces or precedes the rules | Rule misroutes are frequent |
| System-wide actions | Install action | A privileged helper with typed verbs behind one sudoers entry | First install that needs `sudo` |
| Technical contact | Category `access` + extension | Access and login requests go to a second person | A second person exists |
| Second channel | Channel adapter | Another chat platform; answers already apply once | A real need |
| Release tracking | Action + notice | A release command and probe; reports say "released" only with it | The PO wants releases tracked |
| Housekeeping | Journal | Scheduled backup, retention, an external heartbeat URL, a rate limit on notices | The journal grows, or the service fails unnoticed |

Each gets its own short design when its trigger appears, written against the signals the first release has collected.

## 14. The nine scenarios

| # | Scenario | First release | Later |
|---|---|---|---|
| 1 | The PO describes a goal | `/goal <component> <text>`; Shikiru's Leader triages and plans; one *integrated* notice when the family is done | Goal intake |
| 2 | Development hits a technical problem | Retry, quota wait, engine restart (also after a reboot); a missing tool installed after approval; the rest forwarded with what was tried | More actions |
| 3 | The first repair does not work | Loop guard: one automatic action per cause, then forward with the history; a failed check forwards with the output | Further strategies per action |
| 4 | A real product decision | Forwarded with the agent's options and ★; one tap applies it | Charter answers, autonomy |
| 5 | Access or authority is missing | Login problems forwarded with instructions; `sudo` and system installs forwarded as instructions for the PO | Technical contact, privileged helper |
| 6 | The PO wants to know what is happening | `/status`, `/open`, *integrated* notices, pull-request review links, health notices | Digest, acceptance notes, review delegation |
| 7 | An agent becomes unavailable | Shikiru's own ladders first; quota escalations wait for the reset; the Operator's Reasoner down means forward | Spend decisions |
| 8 | The computer or the Operator restarts | Cursors, `ops`, the liveness rule (engines come back after a reboot; a stop the PO made stays respected), observation-based reconcile ([§10](#10-restarts-and-failures)) | Housekeeping |
| 9 | Several channels | The Channel interface with destinations; Shikiru applies only the first answer | Topics, second adapter |

## 15. Proving it works

**Drills**, against Shikiru with its fake agent and against two small real projects:

* A missing tool: approve, install, check, task continues. The same with Decline, a tool that is already there, a hostile package name, and a failing install.
* A transient agent error: one retry, then a forward when it repeats.
* Quota exhaustion: the wait, the message, the retry at the reset; and a reset more than 24 hours away.
* Kill an engine; kill the Operator mid-install, mid-resolve and mid-send; reboot the host and check that engines come back, and that an engine the PO stopped stays stopped across an Operator restart.
* A task in pull-request mode: the review message, the merge, the notice; and a PR closed unmerged.
* Two answers to the same question from two chats, and an answer given in tmux.
* The Reasoner unavailable: everything is forwarded with a note, one health notice, nothing lost; and a ladder whose first rung fails.

**Measures,** always on, from journal timestamps:

| Measure | Why |
|---|---|
| **PO wait time**: escalation raised to resolved, and the share spent waiting for the PO | The idle time the Operator exists to remove |
| **Handled without the PO**, per category and action | Shows which extensions are worth building next |
| **Escalation mix**: count per reason and per Reasoner verdict | Real data to decide the next handlers ([plan.md](plan.md)) |
| **Misroutes**: verdicts reviewed by hand against what the PO and the agents did (first month, every `decision_required`) | Whether classification from text is good enough |
| **Installs**: approved, declined, checks failed | Whether the install path earns its keep |

## 16. Trade-offs and limits

1. **Classification from text, not a typed field.** No Shikiru change and works with every agent. The cost is a model call per `decision_required` and possible misroutes; bounded by "unsure means product", by approval before any host change, and by reviewing verdicts.
2. **The Operator's own Reasoner, outside Shikiru.** A few cheap calls behind a small ladder, with read-only lookup tools. Failure simply forwards, so there is no quota accounting. Their usage is logged by the Operator, not in Shikiru's ledger.
3. **Forward by default.** The first release saves the PO the infrastructure interruptions it can fix and makes every other one answerable from the phone. Product questions still reach the PO in the agent's own words, which a non-technical PO may find hard to answer; plain-language questions are an extension with a measurable trigger.
4. **Typed install recipes, not free-form commands.** Safer and simple, but only five user-level package managers are covered, and system managers that need `sudo` are not. Anything else is forwarded as instructions.
5. **The PO approves host commands,** but sees only commands that code built from validated names, so the approval is not a review of model-written shell.
6. **A retry may often fail.** Shikiru has already retried within its own budgets before it escalates. The loop guard bounds the cost, and the mix measure shows whether retries pay.
7. **No cross-component planning yet.** The PO names the component in `/goal`. Splitting one goal over several components is an extension.
8. **A rare duplicate goal.** A crash at the wrong moment can leave a `/goal` uncertain; the PO is asked rather than the Operator guessing.
9. **A stale engine environment.** The engine keeps the environment it was started with, so a tool installed into a new directory may still be invisible to the agents until the engine restarts. The success message gives the full path so the agent can call the tool directly.
10. **Escalation text leaves the host.** Questions go to Telegram automatically, and *Details* sends more of the packet on request. Packets can contain project data. The PO decides what the chat may hold by choosing the bot and its owners.
11. **One host, one channel.** Not highly available. A lost Telegram acknowledgement can produce a duplicate message, which carries its reference.
12. **A clean stop is judged by time.** Shikiru does not record who stopped the engine, so a stop is respected only after `stop_grace_min` with the Operator running. A project the PO stopped while the Operator was down is started again.
13. **An unmerged pull request may cost one retry.** Shikiru raises `integration_blocked` when a PR waits beyond its budget; the first retry just waits again, and the loop guard forwards the second.
