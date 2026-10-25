# Shikiri-yaku: design

Status: current design. No code exists yet. It builds on what the current Shikiru implementation provides ([Shikiru](https://github.com/ferrum-code-dev/shikiru): its `README.md`, `docs/design.md` and the MCP server). This document is the design, not code.

**How to read it.** [§1](#1-idea-in-one-paragraph)–[§13](#13-proving-it-works) are the design: idea, architecture, objects, control loop, the nine scenarios, memory, Shikiru extensions, reasoners and host authority, defaults, trade-offs and the proof plan. [§14](#14-review-what-the-scenarios-missed) reviews it from the PO's side. [§15](#15-implementation-effort)–[§16](#16-assessment-can-shikiru-build-it-and-does-it-boost-development) are the implementation estimate and the feasibility assessment (including the dogfooding protocol).

## 1. Idea in one paragraph

Shikiri-yaku (the **Operator**) is the one party the Product Owner (PO) talks to. The PO states outcomes and makes product decisions. Shikiru does the development work, one engine per repository. Shikiri-yaku sits between them as a small background service. Toward the PO it takes in goals, asks only the questions that change the product, and reports honestly. Toward Shikiru it behaves like a very attentive human Operator. It submits work, watches events, answers escalations it is entitled to answer and repairs the machine when development stalls. It does not plan, schedule or store the backlog. Its job is to remove idle time (waiting for the PO, for a repair, for a restart), because the pace of the work itself is set by Shikiru and its agents. The measure of success is therefore PO wait time and lead time, not the number of automated actions ([§13](#13-proving-it-works)).

**Deployment unit.** One instance serves exactly one **product**. A product consists of one or more **components**; each component is one Shikiru project (one repository). A PO who manages several products runs several instances, each with its own config, journal, service unit, chat bot and owners ([§3.1](#31-one-instance-per-product)). In this document "project" means a Shikiru project, that is, a component of the instance's product.

## 2. Principles

1. **Shikiru is the source of truth for development.** Tasks, plans, reports, sessions, escalations, history, usage and quota, which tasks belong to which goal, and what each project is are read through Shikiru's MCP server. Shikiri-yaku never touches its database and never keeps a copy of the backlog or of project knowledge.
2. **Own only what Shikiru cannot own.** That means the conversation with the PO, work that spans projects, the host machine, and the incident memory of this Operator.
3. **Monitoring and communication never depend on a model.** Watching, routing by rule, messaging, digests and recovery are plain code. Models are used to *investigate* and to *interpret*, never to keep the service alive.
4. **Investigate before asking, and proceed before waiting.** The PO is interrupted only for a product decision, or for access and authority that no routine rule covers. "I'm not sure" is a reason to gather evidence, not a reason to ask. A choice that is cheap to reverse is proceeded on and announced rather than waited for, within the autonomy dial the PO controls ([§4](#4-who-owns-what)).
5. **Every side effect is journaled before it happens and verified after.** After any restart the Operator looks at what really happened before it repeats anything.
6. **One decision, one identity, one effect.** A PO decision has a single ID and version across all channels and is applied to Shikiru exactly once.
7. **KISS/YAGNI.** One Python process, one SQLite file, one TOML file, the OS service manager, no queue, no vector store, no agent swarm.
8. **Remove idle time, and measure it.** Shikiru and its agents set the pace of the work. The Operator speeds development by cutting the waiting for the PO, for repairs and for restarts, and it reports PO wait time and lead time.
9. **A gap in Shikiru is a Shikiru requirement, never an Operator workaround.** If the Operator needs something Shikiru does not offer (a field, a tool, a guarantee), the design states it as a requirement in [§9](#9-shikiru-what-is-used-what-is-needed) and assumes it. Shikiru is changed first. The Operator carries no fallback code for a missing Shikiru capability.
10. **Spend tokens where the context already is.** Facts come from typed fields, not from a model reading prose. Questions about a repository go to the agent that already knows it, through Shikiru. Cheap models interpret, strong models investigate, templates report ([§10](#10-reasoners-the-optional-judge-and-host-authority)).

## 3. Architecture

```mermaid
flowchart LR
  PO([Product Owner]) <--> TG[Telegram] & CH2[later channels]
  TG & CH2 <--> CN[Conversation]
  CN <--> J[(Journal<br/>SQLite)]
  W[Watcher<br/>1 MCP link per project] <--> J
  W <-->|stdio MCP| S1[Shikiru: api]
  W <-->|stdio MCP| S2[Shikiru: web]
  W --> T[Triage<br/>rules]
  T --> CN
  T --> C[Case manager]
  C --> R[Reasoner<br/>prompts + schemas]
  C --> H[Host]
  R -->|interpret / inquire| S1 & S2
  T -.optional.-> JM[Judge: Jev or Laya]
  CN & C --> PL[Policy<br/>autonomy dial]
```

One process, asyncio, one event loop. Modules, not services:

| Module | Responsibility | Uses a model? |
|---|---|---|
| **Conversation** | Channel adapters; inbound dedupe and sender authorization; the `outbox` fan-out and notification budget; decision lifecycle and stale/conflict handling; templates for milestones, digests and on-demand status | Only to interpret free text |
| **Intake** | Turns a PO goal into a *brief per component* using the product charter and the components' cards, with testable acceptance criteria, assumptions and the few material questions; keeps the goal queue; submits to Shikiru; sequences work across components | Reasoner (cheap routing, read-only assessment) |
| **Watcher** | One supervised `shikiru --project P mcp` link per project; durable event cursor; snapshot reconcile; engine liveness | No |
| **Triage** | Maps every event to a *route* (ignore, report, decision, technical case, wait) by a rule table on Shikiru's typed fields ([R2](#required-shikiru-changes)). Only an event with no usable type goes to investigation. The Judge only if enabled | No (Judge optional) |
| **Policy** | The autonomy dial: classes, levels, promotion and demotion from track record ([§4](#4-who-owns-what)) | No |
| **Case manager** | Incidents: fingerprinting, evidence, attempt ladder, budgets, repair plans, verification, parking, closure | Via Reasoner |
| **Reasoner** | Holds the prompts and output schemas for intake, explain and investigate, and calls Shikiru's `interpret` and `inquire`. It has no agent runtime, ladder, quota tracking or usage ledger of its own: those are Shikiru's | Yes, through Shikiru |
| **Host** | Maintenance lock, step policy and plan execution, privileged helper, probes (disk, tools, `shikiru doctor`), engine start/stop | No |
| **Journal** | SQLite store ([§8](#8-what-must-be-remembered-between-runs)) | No |

**Why a separate service.** The supervisory lifecycle (monitoring, pending decisions, recovery, message delivery) must stay the same when the reasoning agent changes, runs out of quota or crashes. Tying it to one agent runtime would make exactly the failures in scenarios [7](#7-an-agent-becomes-unavailable) and [8](#8-the-computer-or-the-operator-restarts) fatal.

### 3.1 One instance per product

An instance is a directory (`yaku.toml`, `journal.db`, transcripts), a service unit named after the product, and one bot token. There is no registry of products and no portfolio layer.

| Per instance (isolated) | Shared on the host (small and explicit) |
|---|---|
| Config, owners, destinations, journal, goals, decisions, digests | **Maintenance lock**: one lock file in a host-wide state directory, so two instances never change the environment at the same time. It also makes "one repair plan executing at a time" a host-wide rule |
| Telegram bot and chat: the chat itself tells the PO which product is speaking | **Privileged helper** `yaku-admin` and its policy file, when built |
| Instance lock: one process per product directory | **Component claims**: a claim file per repository path, so two instances cannot supervise the same Shikiru project. Start refuses with a clear message |
| Journal and goals; model use goes through that product's Shikiru projects | Machine capacity and agent subscriptions (see below) |

* **A bot per product.** Telegram allows one long-polling consumer per token, so two instances sharing a token would fight over updates. A bot takes a minute to create and removes all routing logic. Other channels work the same way: one chat or channel per instance.
* **What disappears.** Cross-product digests, "which product?" disambiguation, cross-product sequencing and per-product recipient mapping. A fault, upgrade, rollback or removal of one product cannot touch another.
* **What the isolation costs.** Instances do not see each other's quota blocks, so two products on one subscription each find out about exhaustion on their own, and their Shikiru engines can together exceed the machine. Defaults: a small `max_parallel` per engine, and the host-wide lock for repairs. A shared block file is *Later*, triggered by a second product actually hitting this.
* A combined view across products is outside the scope (YAGNI). The PO simply has one chat per product.

## 4. Who owns what

| Subject | Owner | Operator's access |
|---|---|---|
| Backlog, plans, task state, reports, sessions, review loop, integration, model ladders, quota waits, **usage and spend**, **which tasks belong to a goal** (task origin, [R1](#required-shikiru-changes)) | **Shikiru** | MCP queries and commands only |
| What each component is and how it is built: description, **project card**, declared commands (test, health, preview, release), trackers | **Repositories**, served by Shikiru's `project_info` ([R5](#required-shikiru-changes)) | Read through MCP; changed only by Shikiru tasks |
| Code, `CLAUDE.md`/`AGENTS.md`, docs | **Repositories** | Read-only `inquire` turns that Shikiru runs in the repo |
| **Product charter** (the principles the PO has set) | **The home component's repository**, as a tracker file | Read through MCP; amended by a Shikiru task |
| Goals (PO text, acceptance, queue position, plan of items) | **Operator journal** | Own table. Task ids are not stored: they are queried by origin |
| PO decisions and everything said to or by the PO | **Operator journal** | Own tables |
| Incidents, attempts, learned fixes, autonomy levels | **Operator journal** | Own tables |
| Credentials, agent logins | **Host/agent-native stores** | Never in the journal or in chat |

Product and architecture knowledge therefore has one home, the repositories, which every agent in a component already reads. The Operator never holds a second copy: it reads cards and the charter through Shikiru when it needs them and caches them by content hash. A decision that matters for the code reaches the repository through the work it caused (the code, the tests, the acceptance criteria) or, when it is a standing principle, through a charter amendment.

### Progress vocabulary

All reports use four levels. Shikiru derives the first three and exposes them as a task `level` ([R7](#required-shikiru-changes)), so the Operator never infers them from events:

| Level | Meaning | Shikiru evidence |
|---|---|---|
| **Implemented** | Code written on the task branch | Implementer turn reported `completed` |
| **Verified** | Reviewed and accepted | Leader `verdict` = accept |
| **Integrated** | In the base branch; in PR mode only after the merge has landed and been verified, because Shikiru records completion only after integration settles | Task `done` |
| **Released** | In production | Not known to Shikiru. Reported only if a per-component `release_probe` is configured (*Later*, [§14](#14-review-what-the-scenarios-missed)). Until then reports say "release not tracked" |

A goal's level is the lowest level among its items. Progress is shown as items per level, never as a percentage of a changing task count. **Accepted** is not a progress level but the PO's own sign-off on a goal (scenario [6](#6-the-po-wants-to-know-what-is-happening)); it never blocks dependent work. Until a release probe exists (*Later*, [§14](#14-review-what-the-scenarios-missed)), goals are shown as Integrated with "release not tracked", and reports never say "released".

### Autonomy: the contract and the dial

**The contract**, agreed once at setup, answers "when will you bother me?".

| The Operator handles it alone | The PO (or the technical contact) is asked |
|---|---|
| Retrying work, restarting registered services and engines; installing development packages and repairing configuration from trusted sources (at once on a dedicated host, on a workstation after one-tap approvals until the dial promotes it) | A change to customer-visible behavior, scope, quality, priority or delivery date |
| Diagnosing builds, tests and environment faults; researching errors; verifying repairs | Spending outside the standing budget, including a switch from subscription to metered use |
| Technical questions the brief, the charter and earlier decisions answer, and equivalent implementation choices (announced with a veto window until the dial promotes them) | Credentials, MFA, admin rights or accounts the host does not have (to the technical contact if configured) |
| Switching to an allowed rung within the cost class; waiting for quota resets | A technical problem still unsolved when the attempt or time budget is spent, stated in product terms |

**The dial.** The contract is only the starting position. Every kind of choice the Operator could make alone belongs to a *class*, and each class has one of three levels:

| Level | Behavior |
|---|---|
| `ask` | The PO (or the technical contact) decides. The affected work waits |
| `default` | The Operator announces its recommendation ("Going with A in 15 min unless you object") and applies it after a veto window. The veto is a button on the announcement, which reopens the item at `ask`. A later reversal is a new decision |
| `auto` | Applied at once and listed in the next digest |

| Class | Starts at | Can rise to |
|---|---|---|
| `decision.technical`: equivalent implementation choices | `default` | `auto` |
| `decision.reversible`: product detail that is cheap to undo and costs nothing | `ask` | `default` |
| `decision.scope`: scope, quality, priority, delivery date | `ask` | never |
| `decision.cost`: spend, irreversible changes, access | `ask` | never |
| `repair.retry`: retry, rung change, engine restart | `auto` | n/a |
| `repair.user`: user-level installs and configuration fixes | `ask` (`auto` on a dedicated host) | `auto` |
| `repair.system`: system-wide changes through the helper | `ask` | `default` |
| `prevention`: a task that makes a recurring fix reproducible | `default` | `auto` |

* **Promotion by track record.** After five consecutive items of a class where the PO took the recommendation unchanged (for repairs: the plan was approved unchanged and its probe passed), the Operator offers one tap: "You accepted my recommendation on UI-detail choices five times. Apply automatically from now on?" A refusal is not repeated for 30 days. Classes marked "never" can only be changed in config.
* **Demotion.** A veto, a reversal or a failed probe moves the class down one level at once, and the Operator says so.
* **Away mode** is a modifier. While the PO is away ("away until Monday"), reversible, costless `ask` items are treated as `default` with a longer veto window (4 h). Scope, cost, irreversible and access decisions always wait.
* **Where the classification comes from.** The escalation itself ([R2](#required-shikiru-changes)): the agent that asked, which holds the context, states the `category`, what the answer `changes`, and whether each option is `reversible` and what it costs. The Operator records these with the decision and maps them to a class by rule; no second model re-judges them. A misclassified decision is the dial's main risk ([§14](#14-review-what-the-scenarios-missed)), which is why classes start low and demotion is immediate.
* Levels live in the journal (`policies`). Config sets the starting levels and the ceilings.

### Product charter

A one-page file in the **home component** (`home` in `yaku.toml`, default the first component), kept in git and listed in that project's trackers, so every agent of that component reads it with its normal context and every change is a reviewed commit. It is written once in a 15-minute onboarding chat: `inquire` drafts answers to about ten questions from the components' cards and docs, each with a recommended answer, and the PO taps "ok" or corrects. The Operator then submits one Shikiru task that commits the text. Content: who the users are, the quality bar, hard constraints (budget, compliance, stack), non-goals, default technology choices, test policy, deployment expectations, and how cautious to be.

It does three jobs. Intake treats anything the charter answers as an assumption, not a question. The Leaders of the home component already apply it, which cuts their questions at the source. And any later decision flagged as a precedent proposes a one-line amendment ("add to the charter?"), committed by a Shikiru task. Each brief to another component carries the relevant extract with the charter's commit hash, which is the only copy outside the repository and is refreshed when the hash changes. Most questions from Leaders and from Intake come from missing principles, so this is the cheapest way to cut them.

## 5. Core objects

Small, durable, and all in the journal. IDs: `G-n` goal, `D-n` decision, `I-n` incident. Project work is addressed as `api/T-12`.

* **Goal**: PO text, state (`clarifying → queued → active → done | cancelled`; `done` means every item is Integrated, and the PO's sign-off is a separate `accepted` mark), priority, brief with acceptance criteria each marked *checked by* (automated test, command, or PO), assumptions, and its *items*: `(component, brief, after_item?)`, the plan of what to submit where. An item's **origin** is `G-4/api` (goal and component). It is sent with the task as its origin and idempotency key ([R1](#required-shikiru-changes)). Shikiru returns the item's tasks, levels, usage and times by origin, including tasks its Leader created later, so the journal stores no task ids.
* **Decision**: `kind` (`product | technical | access | spend | fix`), question in product terms, options with consequences and `reversible` and `cost` flags, recommendation, `recipient` (the owner or the technical contact), `state` (`open → answered → applied`, or `stale`, `withdrawn`), `version`, a veto deadline for `default` items, a reference (Shikiru escalation `E-n` or an Operator-originated block), the policy class and level it was handled at, who decided (`po | contact | default | auto`), the verification probe for `access` and `fix`, and the audit trail of who answered through which channel.
* **Incident**: `fingerprint`, scope (project/task/host), class, state (`investigating → repairing → verifying → watching → resolved`, or `parked`, or `needs_owner`), budget used, attempts.
* **Attempt**: approach, hypothesis, evidence refs, the repair plan (ordered steps) and its execution summary with transcript path, verification probe and result, outcome (`fixed | no_effect | worse | inconclusive`), and "what is different from the previous attempt".
* **Policy**: `(class, level, ceiling, streak, last change)`, one row per autonomy class ([§4](#4-who-owns-what)).
* **Charter**: the versioned product principles ([§4](#4-who-owns-what)).
* **Op**: the intent journal. Every command to Shikiru and every host mutation is an Op: `intended → issued → confirmed | failed`, with its idempotency key and the verification condition (scenario [8](#8-the-computer-or-the-operator-restarts)). A Shikiru command is re-issued with the same key after a restart; a host mutation is verified first.

## 6. The control loop

```
Shikiru events ─► Watcher ─► Triage ─► route
                                        ├─ report     → Conversation (milestone / digest)
                                        ├─ decision   → Policy → Conversation (ask, or default / auto per the dial)
                                        ├─ case       → Case manager → Reasoner/Host → verify → resume Shikiru
                                        └─ wait       → park with a wake-up condition
```

**Event intake.** The Watcher long-polls `wait_for_events(since_id)` per project. A cursor advance and the journal rows it causes commit in one transaction, so a crash replays at most one batch and processing is idempotent by event id. Shikiru's `notify.command` hook is not used: it is fire-and-forget and loses informational events. If the cursor falls behind Shikiru's retention, the Watcher reconciles from `status`, `list_tasks` and `list_escalations`.

**Triage, in order.**
1. *Rule table* on Shikiru's own typed fields: escalation `reason` (`agent_auth`, `quota_exhausted`, `integration_blocked`, `engine_error`, …), event kind (`engine_fault`, `quota_blocked`, `task_done`, …), `needs_input`, and for agent questions the `category` and `changes` that the asking agent sets ([R2](#required-shikiru-changes)). Every Shikiru-originated event ends here, at no token cost.
2. *Investigate* when a type is `other` or an event matches no rule. The case manager gathers evidence and asks `inquire` in the affected project to diagnose it. Low certainty means *more evidence*, never a PO question.

The optional Judge ([§10](#10-reasoners-the-optional-judge-and-host-authority)) could second-guess the agents' `category` and `changes` if replays of real escalations show they are often wrong.

A third route covers agent questions that are neither product nor environment (`category` = `technical`), such as which of two equivalent implementation approaches to take. The asking Leader has already weighed them, read the charter (it is one of its trackers) and starred a recommended option. The Operator answers by choosing that option through `resolve_escalation(option_id)`, with no model call, when `changes` is empty and the option is `reversible`. This is class `decision.technical` ([§4](#4-who-owns-what)): at `default` the PO sees "Going with X" and can veto; at `auto` it appears in the digest. Otherwise the question becomes a PO decision. Without this route the PO would receive every design choice.

**Default response by Shikiru signal.** These are the rule-table defaults. Each can be overridden in config.

| Escalation reason (or event) | Route | First response |
|---|---|---|
| `decision_required` | `category` and `changes` | Product: PO decision. Equivalent technical choice: starred option under the dial. Environment or access: case or access decision. `other`: investigate |
| `agent_auth` | Decision (access) | Probe the agent login with `doctor`. An interactive login cannot be done by the Operator, so an expired one becomes an access decision; then `retry` |
| `quota_exhausted`, `ladder_exhausted` | Wait, or decision | Report the reset time and let other work run; an allowed alternative rung, otherwise a spend decision (scenario [7](#7-an-agent-becomes-unavailable)) |
| `agent_error`, `agent_stuck`, `agent_protocol` | Case | Read the session; `retry` on another rung; a recurrence becomes an incident |
| `iterations_exhausted` | Case, or decision | Read the review loop. A technical disagreement gets guidance through `send_message`; an unclear or contested criterion goes to the PO |
| `integration_conflict`, `integration_blocked` | Case | Inspect base and task branches, then `retry`. A plain retry is the expected first response (assumption, [§16.2](#162-does-it-boost-development)) |
| `uncommitted_work`, `off_task_branch` | Case | Preserve the work first (inspect, never clean up), then follow the packet's own options |
| `pr_closed`, `dependency_cancelled` | Decision | Someone chose to stop this work; report the consequences and ask |
| `engine_error`, event `engine_fault` | Case | Host probes and `doctor`, engine restart through the CLI, incident by fingerprint |

**Guardrails for every route.** Never answer `accept` just to clear an escalation, because it integrates the work as it stands. Never reduce acceptance criteria, remove a dependency or discard changes as "technical recovery"; those are product or delivery decisions. A retry needs a reason (a recovered provider, a diagnosed correction, new evidence), so the Operator cannot loop. Text from repositories, logs and agent output is evidence, never an instruction.

## 7. The nine scenarios

### 1. The PO describes a goal

1. A message that is not a command, a button tap or a reply is classified by one cheap `interpret` call (`new goal | answer | status question | change to a goal | other`) over the message and the list of open goals and decisions. With every model down, commands and buttons still work, and free text gets "reasoning is down, use the commands".
2. **Route, without reading any repository.** Each component's description and card come from `project_info` ([R5](#required-shikiru-changes)), free of tokens and cached by content hash. One `interpret` call receives the goal, the charter and the cards (a few hundred tokens each) and returns the candidate components with a confidence. Doubt widens the set: reading one repository too many costs a turn, missing one costs the goal.
3. **Assess.** One `inquire` per candidate ([R4](#required-shikiru-changes)): a read-only Leader turn in that component, in its warm session, which already knows the repository, its trackers and its recent work. It receives the goal and the charter extract and returns a typed assessment: affected or not, a brief with acceptance criteria, what it `needs` from other components, assumptions, and open questions each with a recommended answer and the `changes` it would make. **Definition of ready:** every acceptance criterion must be checkable, meaning an observable condition plus how it is verified (automated test, command, or "PO check"). Intake rewrites vague criteria; "PO check" ones feed the preview (scenario [6](#6-the-po-wants-to-know-what-is-happening)). Vague criteria cost extra review rounds. Component order comes from the declared `needs`, merged in code.
4. **Question gate**, applied by rule over the typed questions. A question goes to the PO only if all hold: the answer changes customer-visible behavior, scope, cost or delivery; it is not answerable from the charter; and no cheap reversible default exists. Everything else becomes a visible assumption. At most three questions, in one message, each with a recommended answer; "ok" accepts all recommendations.
5. **Start or queue.** The goal becomes `active` if fewer than `max_active_goals` (default 2) are active. Otherwise it is `queued` in the PO's priority order ("do G-5 first") and the PO is told its position. Assessment and its questions run at once, so they are answered while earlier goals are still running. Without blocking questions an active goal is submitted at once, and the Operator tells the PO what it understood, what it assumed and where work started. With blocking questions it submits the unaffected components and holds the rest.
6. **Submission.** One `submit_task` per component carries the *outcome-level brief*, not technical tasks, with origin `G-4/api` and that origin as its idempotency key ([R1](#required-shikiru-changes)). Shikiru's Leader triages and decomposes (its `plan` mode) and its child tasks inherit the origin. Every item is submitted at once. An item that depends on another component is submitted **held** ([R3](#required-shikiru-changes)) and released when its prerequisite is **Integrated**, because Shikiru task branches start from the base branch. Shikiru's `depends_on` is per project, and Shikiru runs one engine per repository, so this one release rule is the Operator's only scheduling duty. Because every item exists in Shikiru from the start, the goal's whole plan is visible there, and a restart has no deferred submission to recover.

### 2. Development hits a technical problem

Typical sources: an `escalation` with a technical reason, an `engine_fault`, an Implementer blocked on a tool, repeated `iterations_exhausted`, or a stall that Shikiru has exhausted its own continuation attempts on (Shikiru detects stalls itself and escalates).

1. Triage opens or **reopens by fingerprint** an incident. The fingerprint is project, reason, step and a normalized error signature (paths, ids and numbers stripped).
2. **Quiesce only if needed.** Additive fixes (installing a missing package) do not stop work. Changes that replace a toolchain version under running tasks do: the Operator pauses the project (`pause_project`) or, for one task, holds it, with itself as the hold's owner ([R3](#required-shikiru-changes)). It resumes only holds it owns, so a pause the PO set by hand survives. A full stop uses `engine_stop` ([R6](#required-shikiru-changes)), which returns after the lock is released. It confirms from `status` that the work has really stopped before the change starts; an accepted pause command is not enough.
3. **Evidence first**, collected by plain code: the escalation packet and its "what was tried" digest, recent events, `peek_session`, the structured `doctor` result ([R6](#required-shikiru-changes)), disk and memory, tool versions, the failing command re-run in the task worktree. Evidence stays where it is: the Operator sends the diagnosing agent a pointer to the task, not a copy of the logs.
4. **Plan, then execute.** Product-code problems are not repaired by the Operator. They return to Shikiru as guidance (`send_message` plus `retry_task`/`resolve_escalation retry`) or as a new Shikiru task. For host problems (missing system library, broken PATH, full disk, stopped service) an `inquire` in the failing task's project ([R4](#required-shikiru-changes)) returns the diagnosis, a **repair plan** (ordered steps, each marked reversible or not), the **verification probe**, and what differs from earlier attempts. No agent holds host rights. The Host module checks the plan against the step policy ([§10](#10-reasoners-the-optional-judge-and-host-authority)), and the plan is applied according to the dial class `repair.*` ([§4](#4-who-owns-what)): at `ask` it goes as a *fix* decision to the technical contact or PO (diagnosis, exact steps, probe, one tap to apply); at `default` it is applied after the veto window; at `auto` at once. The Host module executes it step by step under the maintenance lock.
5. **Verify** with a probe declared *before* acting and executed by the Operator, not self-reported: the failing build or test command passes, then Shikiru resumes. The incident moves to `watching` and closes only after the task passes the failed step or a watch window elapses without recurrence.
6. At level `auto` the PO sees nothing at the time; the repair appears in the next digest, immediately only if it delayed delivery by more than an hour. At `ask` and `default` it was the one-tap decision above, and each unchanged approval counts toward promotion ([§4](#4-who-owns-what)).

### 3. The first repair does not work

* An incident has an **attempt ladder** with a hard budget (5 attempts, 2 hours, and a spend cap if a metered rung is used). Reopening by fingerprint never resets it.
* **Attempts must differ.** A plan is rejected if its `(approach, target)` equals a failed attempt and it cites no new evidence.
* **Strategy escalation:** (a) remembered fix for the same fingerprint, if any; (b) primary rung with the evidence bundle; (c) same rung with a deeper evidence pass (full logs, dependency tree, system journal) and the explicit list of failed attempts; (d) a stronger rung of the inquiry ladder, requested explicitly (Shikiru already moves on by itself when a rung is blocked); (e) external help via an access decision, or `needs_owner`.
* The case file is re-rendered from the journal for every attempt, so changing agent loses nothing and no native session needs to be portable.
* **Fencing.** Every attempt has a number. The result of an attempt that is no longer current (a slow agent that timed out and was replaced) is discarded and never applied. Before a replacement starts, the previous attempt's uncertain host changes are reconciled.
* Exhausting the budget gives a short owner message in product terms: what is blocked, what was tried, what is needed or which decision unblocks it. It is still not a request that the PO run commands.
* **Fix once.** A fingerprint that recurs within 14 days leads to a decision-free Shikiru task that makes the setup reproducible (a bootstrap script or a documented prerequisite), under class `prevention`. Repairing the same thing repeatedly is the failure mode.

### 4. A real product decision

Sources: a `decision_required` escalation whose `category` is `product` ([R2](#required-shikiru-changes)); an Operator finding that the goal is infeasible under current constraints (for example it needs a paid service or breaks an earlier decision); or a proposed change to scope, quality, priority, cost or delivery.

1. The **Decision** is written in product terms: the issue, 2–3 feasible options with consequences (behavior, effort/time, cost, risk), and a recommendation. For a Shikiru escalation these come from the packet itself: the asking agent writes the `owner_summary` and the options' consequences in product language ([R2](#required-shikiru-changes)), and the Operator fills a template, so no model call is needed. Technical detail stays one tap away ("details"). An illustration (invented content):

   > **Shop · invoice export · D-7**
   > The payment provider cannot supply invoices older than 90 days. I checked the API and tried a fallback; neither covers the full requirement.
   > A. Ship export for new invoices now, add history later.
   > B. Keep full history in scope and move delivery while a second data source is added.
   > C. Pause export and start the next feature.
   >
   > Recommended: A, if history can follow separately. New-invoice work continues; history is on hold until you answer.
2. **The dial.** Before asking, Policy looks up the decision's class ([§4](#4-who-owns-what)). `ask`: continue as below. `default`: the message says "Going with A in 15 min unless you object" and the Operator applies the recommendation after the veto window; this is the Operator's own decision, taken under authority the PO granted and announced. `auto`: applied and listed in the digest. Scope, cost, irreversible and access decisions are always `ask`.
3. **Waiting without blocking.** The affected Shikiru task is already `escalated` and frees its slot. Unaffected tasks and other projects continue. For Operator-originated decisions the Operator holds only the affected items.
4. **No material change before the answer.** Shikiru's Leader may change scope. With `scope_change = hold` ([R3](#required-shikiru-changes)), the tasks it creates start held and the Operator releases or cancels them after the decision, so nothing material happens before the PO has answered.
5. **Applying.** Chosen option → Shikiru command: for an escalation, `resolve_escalation(option_id, note)`, which names the option the agent offered, so the Leader receives the choice itself and not prose regenerated from it ([R2](#required-shikiru-changes)). Silence never approves an `ask` decision. Unanswered `ask` decisions are re-sent as reminders (default after 4 h, then daily).

### 5. Access or authority is missing

* **Standing authority** is the dial's starting position ([§4](#4-who-owns-what), [§10](#10-reasoners-the-optional-judge-and-host-authority)): retries, engine restarts and switching to *allowed* rungs within the cost class never prompt, and on a dedicated host neither do user-level repairs. Other repairs start at `ask` and are promoted by track record.
* When something outside it is genuinely required, the Operator creates an **access decision** (kind `access`) with an exact request (what, where, why, the verification probe), alternatives (a mock or stub, another provider, a narrower scope, waiting) and the delivery impact.
* **Routing:** if a technical contact is configured and `authorized_requests` covers this kind, the decision goes to them as recipient through their own channel, the PO is told the impact ("web is waiting for X from the technical contact; api continues"), and the contact's "done" is accepted only after the probe passes. Otherwise the PO gets a short decision: authorize, choose an alternative, or accept the delay.
* **Secrets never travel through chat.** The request tells the contact where on the host to put them. An inbound message that looks like a secret is rejected with instructions and, where the channel allows, deleted.

### 6. The PO wants to know what is happening

* **Milestones** (immediate, templated from queried state, within a notification budget of 3 a day beyond decisions and access requests; the rest are folded into the digest): goal accepted and started; each component reaching a new level; goal Integrated; a decision needed; an access request; a delivery slip caused by a blocker older than an hour.
* **One digest** at 09:00 on weekdays, skipped if nothing changed. Sections: *Completed outcomes* with levels, *In progress*, *Queued*, *Blocked*, *Technical problems resolved* (one line each), *Decisions the Operator took under the dial* (with how to change them), *Decisions needed*, and a one-line *Usage* summary per goal (Shikiru's usage aggregated by the goal's origin, plus `status.quotas`, with unknown values shown as unknown). On Mondays a *Flow* line adds PO wait time and lead time ([§13](#13-proving-it-works)). Wording comes from templates and live queries, so digests work with every model down.
* **Acceptance.** When a goal is Integrated the PO gets an acceptance note ([§14](#14-review-what-the-scenarios-missed), gap 2) ending in **Accept** and **Change**. Accept stamps the goal as accepted; Change opens a linked follow-up goal. A goal that is not yet accepted never blocks other work, and is reminded once, then weekly.
* **On demand:** `/status`, `/goal G-4`, `/why api/T-12`, `/problems`, `/decisions`, `/flow`, plus free-text questions. Details are fetched live (`get_task`, `peek_session`) and summarized by `interpret` when available, or shown as structured facts when not.
* Reports state the level of every claim. "Done" is never used without a level, and "Released" appears only with a configured probe ([§4](#4-who-owns-what)).

### 7. An agent becomes unavailable

Two different things can fail, and the Operator keeps them apart.

* **Shikiru's own agents** (Leader/Implementer). Shikiru already waits for quota resets, climbs its ladders and escalates `quota_exhausted`/`ladder_exhausted`. The Operator reports the wait with the reset time from `status.quotas` and lets unaffected work run. It intervenes only when Shikiru escalates, and then chooses between an already-configured alternative rung (`set_rung`/`retry`), waiting, or a **spend decision** to the PO.
* **The Operator's own reasoning.** It runs through Shikiru's `interpret` and `inquire` ([R4](#required-shikiru-changes)), so the ladder, the quota blocks and the waiting are Shikiru's, the same machinery as for development. When every rung is blocked, Shikiru returns its normalized failure kind (`QUOTA` with `resets_at`, `UNAVAILABLE`, `AUTH`, `OTHER`) and the incident is `parked` with a wake-up time. Watching, messaging, digests, rule triage and recovery keep running, so "reasoning is down" is itself reported ("investigation paused until 14:10; monitoring continues").
* **Spending is never switched silently.** Shikiru's inquiry ladder skips metered rungs unless an allowance is configured (default none). Where only a metered rung is left, the call returns `SPEND_REQUIRED` with the rung and an estimate, and the case becomes a PO decision with "wait for reset" as the recommended option. Metered use is read back from Shikiru's usage, so the Operator keeps no spend ledger.

### 8. The computer or the Operator restarts

On start the Operator takes a single-instance lock and then, before accepting new work:

1. **Orphans.** Host-helper and plan-step children recorded in `ops` (pid plus start time) are terminated if still alive. Inquiries run inside Shikiru, so none is the Operator's to terminate; one that was in flight is re-asked with the same key.
2. **Reconcile every non-final Op by observing, not repeating:**
   * *Any Shikiru command* (submit, resolve, retry, cancel, hold, message): re-issue it with the same idempotency key ([R1](#required-shikiru-changes)). Shikiru returns the original acknowledgement if it already took the command, so no read-then-decide logic is needed.
   * *Host change*: each plan step is its own Op. Run the Op's verification condition first; if it passes the Op is `confirmed`; if not, the next attempt is told "interrupted, inspect before acting" and package-manager state is checked before any reinstall.
   * *Outbound message*: the Bot API gives no delivery receipt, so an uncertain message is re-sent with its stable reference (`D-12`) to make a duplicate recognizable.
3. **Engines.** `status.engine_up` per project. An engine that is down *without* a recorded `engine_stopped` event died or was killed (host reboot, OOM) and is started with `engine_start` ([R6](#required-shikiru-changes)); a recorded stop is respected, and one the Operator itself requested is told apart from a fault by the `by` field. A fault stop (`engine_stopped` with a `reason`) is an incident.
4. **Decisions** still `open` or `answered-not-applied` are resumed. Open ones are re-sent only if they were not confirmed delivered.
5. A short "back online after Nm" notice is sent if the outage exceeded 10 minutes. An optional external heartbeat URL covers the case in which the Operator itself cannot report.

### 9. Several channels

* A **destination** is `(channel, chat, role)`. All milestones, digests and decisions are enqueued once per destination in `outbox`, carrying the object's ID and version. Targeted replies (answer to a question) go only to the asking destination.
* A reply arrives as an `inbound` row (channel, message id, sender). The sender must be an authorized owner. The reply names its decision and version (button payload `D12.v2.B`, reply-to mapping, or interpretation of free text).
* **First valid answer wins** through one conditional update, `UPDATE decisions SET state='answered' … WHERE id=? AND version=? AND state='open'`. The winner is enqueued as an Op and applied to Shikiru once.
* A later answer gets a precise reply: *"Already decided: B, via Telegram at 12:03 (applied)"*. If the decision was only answered, not applied, a conflicting reply is refused the same way. Changing an applied decision is a **new** decision with its own consequences.
* A reply to an old version, or to a decision whose facts changed or that Shikiru already resolved, is answered with *stale* plus the current question.
* Free-text answers are mapped to an option by `interpret` (or the Judge if enabled). Anything below the confidence threshold is echoed back for a one-tap confirmation rather than applied.
* After `applied`, every destination's original message is edited to the final state (buttons removed). Channels that cannot edit get a follow-up line.

## 8. What must be remembered between runs

One SQLite file (WAL), written only by the Operator, backed up nightly ([§11](#11-defaults)). Everything else is read from Shikiru.

| Table | Contents | Why it must survive |
|---|---|---|
| `cursors` | Last consumed event id per project, snapshot time | Resume without losing or replaying events |
| `goals`, `items` | PO text, brief, priority and queue position, assumptions, the plan of items with their origins, announced levels, acceptance | The PO's intent and the cross-project plan; no repeated milestones. Task ids and progress are queried by origin |
| `policies` | Autonomy level, ceiling and streak per class | The PO's standing choices and earned autonomy |
| `knowledge_cache` | Cards and charter extract by content hash | Only to avoid re-reading; rebuilt from Shikiru at any time |
| `decisions`, `options` | Kind, recipient, question, options with flags, version, state, veto deadline, policy class and level, who decided where, application Op, probe | Unanswered decisions, exactly-once application, track record |
| `inbound`, `outbox` | Per-destination messages, delivery state, platform message ids | Dedupe, edits, retries per destination |
| `incidents`, `attempts` | Fingerprint, budget, every attempt with evidence and verification | Avoid repeat failures; continue after agent change or restart |
| `ops` | Intent journal for Shikiru commands (with their keys) and host mutations, pids | Safe restart |
| `fixes` | Fingerprint → the repair plan that worked and the conditions it relied on | Fast known-fix path |

Not stored: tasks and their links to goals, plans, reports, transcripts, escalation packets, logs, usage, quota blocks (they stay in Shikiru and are fetched on demand), and the charter (it lives in the home repository). Retention: closed incidents and delivered messages 90 days; decisions, policies and fixes indefinitely.

## 9. Shikiru: what is used, what is needed

The Operator drives Shikiru only through its MCP server. Where Shikiru lacks something the Operator needs, the answer is a Shikiru change, not Operator code that compensates ([principle 9](#2-principles)). Shikiru is the user's own project and the Operator is its main client, so the interface is shaped to fit the client.

### Used as it is

| Capability | Used for |
|---|---|
| MCP `submit_task` (priority, `depends_on`, mode, complexity), `prioritize_task` | Starting component work |
| MCP `wait_for_events` with cursors | Event intake |
| MCP `status`, `list_tasks`, `get_task`, `list_escalations`, `get_escalation`, `peek_session` | Facts, evidence, reporting |
| MCP `resolve_escalation`, `retry_task`, `pause_task`, `resume_task`, `cancel_task`, `set_rung`, `send_message` | Acting on cases and decisions |
| Escalation packets (reason, options with consequences and ★, `needs_input`, "what was tried") | Decision text and incident evidence |
| Durable command queue while the engine is down, engine reconcile on start | Restart safety |
| Quota blocks, ladders, usage with provenance in `status` | Scenario [7](#7-an-agent-becomes-unavailable) and cost reporting |
| Stall detection and bounded continuation | The Operator adds no stall watchdog |
| Trackers: files that agents read and update as work proceeds | Charter and project cards |
| The Helper (headless, schema-only) and Leader roles, their ladders, the read-only plugin profiles, `FailureKind` | Base for R4 |

### Required Shikiru changes

These are requirements. The design assumes all of them, and none has a fallback in the Operator. Each row names the workaround that it replaces.

| # | Requirement | Replaces | Size |
|---|---|---|---|
| **R1** | **Origin and idempotency.** `submit_task` takes an `origin` (opaque reference such as `G-4/api`). Tasks the Leader creates, replacements and rework inherit it. `list_tasks`, `get_task`, events and `status` filter by origin, and usage and times aggregate by origin. Every mutating tool and both inquiry tools take an optional `key`; a repeated key returns the original acknowledgement or recorded result | A marker inside the request text, a stored goal-to-task link table, read-then-decide reconciliation after a restart, per-goal usage bookkeeping | Medium |
| **R2** | **Typed, owner-readable escalations.** Leader escalations and Implementer `blocked` outcomes carry `category` (`product`, `technical`, `environment`, `access`, `other`), `changes` (any of behavior, scope, quality, cost, delivery), an `owner_summary` in product language (no code, at most about 400 characters), and for every option an `id`, a consequence, `reversible`, `cost` and the ★ recommendation. `resolve_escalation` accepts an `option_id` and a note. The `escalation` event carries the complete packet | A second model re-classifying free text, a model rewriting questions into product language, regenerating prose from a chosen option, an extra `get_escalation` per event | Medium |
| **R3** | **Operator-controlled holds.** `submit_task` can start a task held. `orchestration.scope_change = "hold"` starts tasks that the Leader creates held and lists their titles and reason in the `scope_change` event. Holds record an `owner` and a `reason`, and are released by their owner. `pause_project(owner, reason)` and `resume_project(owner)` stop and restart dispatch with the engine and tasks kept | Learning of scope changes after the fact and cancelling them, deferred submission of dependent items, "resume only what I stopped" by memory, a coarse engine stop | Small to medium |
| **R4** | **Inquiries.** `interpret(prompt, schema)` is one headless, tool-less Helper-role turn with a cheap ladder. `inquire(prompt, schema, task_id?)` is a read-only Leader-role turn in the project's base checkout, or in the task's worktree, session and run files, using the Leader's warm session where there is one. Both validate the answer against the given JSON schema, return the result with usage or a normalized failure kind, run in the MCP process (no running engine needed), record an event under the caller's origin, and skip metered rungs unless an allowance is configured (otherwise `SPEND_REQUIRED` with the rung and an estimate). Read-only is enforced by the agent plugin, not by prompt | The Operator's own agent runtime: profile ladders, quota blocks, curated environments, usage and spend ledgers, a signal consumer over `start_turn`, and cold repository sessions for intake and investigation | Medium to large |
| **R5** | **Project knowledge.** `project_info` returns name, description, base branch, integration mode, declared commands (`test`, `health`, `preview`, `release`, `release_probe`), trackers with content hashes, and the project's **card**. The card is a tracker, designated in `project.card`, that says what the component is and how it is structured, and the Leader keeps it current as part of accepting work, as it does for any tracker. `read_tracker(path)` returns a configured tracker's content and hash, capped in size | Operator-side component descriptions and commands, cold repository reads to find out what a component is, a second copy of project knowledge | Small |
| **R6** | **Lifecycle and diagnostics over MCP.** `engine_start`, `engine_stop(reason)` and a structured `doctor(probe?)` work for a project whose engine is down. `engine_stopped` events carry `by` (`operator`, `human`, `fault`) and the reason | Parsing CLI output and the CLI as a second interface | Small to medium |
| **R7** | **Derived facts.** `TaskSummary` carries `level` (`implemented`, `verified`, `integrated`) and the time spent escalated. `task_done` is recorded only after integration settles, in PR mode after the merge lands | Inferring levels from events, summing waits | Small |
| **R8** | **A versioned contract.** `contract_version` in `status` and `project_info`, additive-only changes within a major version, and a changelog | Probing the tool set to detect an incompatible Shikiru | Small |

**Order.** R1, R2, R6, R7 and R8 gate Phase A (the relay). R3, R4 and R5 gate Phase B (goals). Phase C needs R4 only ([§15](#15-implementation-effort)). Each is a small, testable Shikiru task, built under Shikiru's own review loop ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)).

## 10. Reasoners, the optional Judge and host authority

**Reasoner.** A thin module with no agent runtime. It holds the prompts and the output schemas for the Operator's model jobs and sends every one through Shikiru ([R4](#required-shikiru-changes)). Shikiru's plugins, ladders, quota waits, failure kinds and usage ledger therefore serve the Operator exactly as they serve development: Claude and Codex, subscription *or* API login, replaceable by configuring Shikiru, with nothing duplicated here.

| Job | Call | Role and ladder | What it reads |
|---|---|---|---|
| Interpret: classify PO text, map a free-text answer to an option, route a goal to components, explain a status | `interpret` | Helper, a cheap rung | Only what the prompt contains |
| Assess: a component's brief and questions, acceptance evidence | `inquire` | Leader, read-only, warm session | The repository and trackers, which the Leader already knows |
| Investigate: diagnosis, repair plan, probe | `inquire` with `task_id` | Leader, read-only | The task's worktree, session and run files |

All jobs are read-only. No agent holds host rights; plans are executed by the Host module (below). Each call has an input cap from config, and its result is data that is checked against its schema, never an instruction. Billing mode is Shikiru's concern: usage records its provenance, and a metered rung is used only within a configured allowance. A blocked ladder returns Shikiru's failure kind and the incident is parked ([scenario 7](#7-an-agent-becomes-unavailable)). Startup checks use the free `doctor` probe ([R6](#required-shikiru-changes)), never inference.

**Where the tokens go.** The design spends them only where the context already is.

* **Typed fields instead of reading prose.** Triage, decision text, the technical-choice route, progress levels and per-goal usage come from fields Shikiru provides ([R1, R2, R7](#required-shikiru-changes)). They cost no tokens and cannot be misjudged by a model with less context.
* **Cards instead of repository scans.** Routing a goal reads each component's card for free and spends one cheap call over a few hundred tokens per component. Only the shortlisted components are assessed.
* **Questions go to the agent that knows.** `inquire` reuses the Leader's warm session and its trackers, and the Operator sends a pointer to the task, not the logs.
* **Cheap interprets, strong investigates.** Classification and mapping run on the Helper's cheap rung, while repository reasoning runs on the Leader's.
* **No call where a rule will do.** A starred, reversible, technical option is applied without asking a model; reports, digests and decision messages are templates over structured fields.
* **Bounded and reusable.** Input caps per call, the attempt budget per incident, and assessments reused while the goal text, the charter hash and the card hash are unchanged.
* **Visible.** The digest shows tokens and spend per goal from Shikiru's usage by origin, so the assumptions about cost in [§16.2](#162-does-it-boost-development) can be measured.

**Judge (optional, off by default).** The brief asks for decision models where they simplify triage and routing. With typed escalations ([R2](#required-shikiru-changes)) triage is a field lookup, so they simplify nothing in the first release, and the Judge is built as an interface only. It adds a hosted dependency, data leaving the host and a calibration effort. The interface stays so that it can be switched on if replays of real escalations show agents mislabeling `category` or `changes` often enough to matter. One call, `judge(digest, questions) → choice/noul with probability and confidence`, over a compact redacted digest (headline, reason, tried-lines, option titles) of about 1,000 tokens at most. Both candidates provide the needed primitives (`choice`, `score`, `noul` with calibrated probabilities). The checked facts about the two backends:

| | Jev | Laya |
|---|---|---|
| Form | Hosted model behind an API (the docs reviewed do not describe local hosting) | Local Python package; PyTorch and Transformers; checkpoints about 0.3–0.4B parameters, up to ~2.3 GB downloaded; 512–1024 token context |
| Cost to deploy | API key, network, data leaves the host | No account; heavy install; CPU/MPS/GPU |
| Fit | Light install | Offline and no-egress |

**Default: off.** If enabled: Jev when a key is configured, Laya as a config switch behind the same interface. The Judge only advises routing. It never authorizes an action, never answers the PO and never changes the digest levels. Per backend, thresholds are calibrated on shadow-mode replays of past Shikiru escalations before it is allowed to act; pricing and data-retention terms of Jev should be confirmed before enabling it. If the Judge fails, rules and the reasoner continue.

**Host authority.** The same trust model as Shikiru's agents: the managed development host is a trusted machine. **No Operator agent holds host rights.** Agents investigate and propose; the Host module executes.
* **Host mode** in config. `workstation` (default; a personal machine): user-level installs only, repairs of class `repair.user` start at `ask`, and anything system-wide or touching services outside the registered ones becomes an access decision. `dedicated` (a VM or build machine): `repair.user` starts at `auto`.
* **Plan execution.** The Host module runs an accepted plan step by step as the dev user, under the maintenance lock, in a scratch directory outside the product repositories. Each step is an Op with its own verification condition. A **step policy** checks the plan before it is shown or run: allowed executables (package managers, git, language toolchains, service managers), trusted package sources only, installs in user or project scope, no pipe-to-shell, no writes outside registered paths, no credentials in the environment. User-level package managers (Homebrew, language toolchains) are freely available.
* **System-wide steps go through one privileged helper**, `yaku-admin`, installed once by an administrator behind a single scoped sudoers entry. Its verbs are typed (`pkg-install`, `service-restart` of registered units), and it reads a root-owned policy file: allowed package managers and repositories, name rules, registered services. Anything else is an access decision.
* Isolation, as in Shikiru, is a convention, not a sandbox. Transcripts and `ops` entries are the audit trail.
* One **maintenance lock** serializes host-mutating work. Read-only investigations may run in parallel (default 2). Conflicting toolchain needs between projects are solved by isolation (per-project versions), not by repeatedly replacing a shared one.

## 11. Defaults

| Area | Default |
|---|---|
| Deployment | `uv tool install`; `yaku install-service --config <dir>/yaku.toml` writes a launchd/systemd unit named `yaku.<product>` with restart-on-failure. `yaku doctor` checks channels, links, helper and compatibility (Shikiru's `contract_version` against the supported range, [R8](#required-shikiru-changes); agent CLIs through Shikiru's `doctor`), because those interfaces change under us |
| Config | One `yaku.toml` per product: product name, components (name, path), `home` component, owners and destinations, timezone, contacts, host mode and authority, schedule, input caps for model calls. Everything about a component beyond its name and path (description, commands, card) is Shikiru's `project_info`, and agent profiles and ladders are Shikiru's configuration |
| Instance | One instance = one product; each Shikiru project is a *component* ([§3.1](#31-one-instance-per-product)) |
| Owners | One responsible owner plus optional delegates, as an allow-list; no voting |
| Telegram | One bot per instance, long polling, an allow-list of user ids, inline buttons, edit-in-place. Updates are saved before the offset advances. Telegram keeps undelivered updates for about 24 hours, so after a longer outage open decisions are re-presented |
| Question gate | Max 3 questions, each with a recommended answer; reversible choices become assumptions |
| Autonomy | Starting levels as in [§4](#4-who-owns-what); veto window 15 min (4 h while away); promotion offered after 5 consecutive unchanged acceptances and not repeated for 30 days; demotion at once on a veto, reversal or failed probe |
| Goals | `max_active_goals` = 2, the rest queued by priority; the PO's acceptance never blocks other work |
| Notices | At most 3 proactive messages a day besides decisions and access requests; the rest go into the digest |
| Digests | One, weekdays 09:00; none if nothing changed. Mondays add a flow line |
| Decision reminders | After 4 h, then daily |
| Incident budget | 5 attempts, 2 h wall time, no metered spend |
| Watch window after a fix | 30 min or the next completed turn on the task |
| Parallelism | 1 repair plan executing (host-wide), 2 concurrent inquiries per instance; a small `max_parallel` (1 or 2) per Shikiru engine |
| Event gap | Resync from snapshots; no assumption that history is complete |
| Retention | Closed incidents and delivered messages 90 days |
| Backup | Nightly copy of `journal.db` through SQLite's backup API to a configured path, 7 copies. Restoring is copying it back; the restart reconcile (scenario [8](#8-the-computer-or-the-operator-restarts)) then runs |
| Heartbeat | An optional external URL pinged every 5 min; recommended in the setup guide, because a service that cannot start cannot report that |

## 12. Consequential trade-offs and limits

1. **Standalone service rather than a plugin in an agent runtime.** More code and one more service, but monitoring, pending decisions and recovery keep their meaning when the agent changes or fails.
2. **MCP only, never the database, and no workarounds.** A stable public contract and no schema coupling. What the MCP lacks is added to Shikiru ([R1–R8](#required-shikiru-changes)), so the Operator carries no compensating code. The price is that Shikiru changes first and its MCP surface grows, which is why it is versioned (R8).
3. **A small own journal rather than state in Shikiru.** Delivery receipts, host repairs and PO conversations do not belong in project databases. Goals, which Shikiru has no concept of, are the Operator's. Their link to tasks is Shikiru's own data through the task origin (R1), so nothing overlaps and the journal can lose its task information without loss.
4. **Agents propose, the Host executes.** No Operator agent holds host rights. A repair is a plan of steps checked against a step policy, shown to a human at the `ask` level and verified by a probe. The cost: exploratory trial and error takes several attempts (each attempt's read-only findings feed the next), and plans are shell steps rather than typed verbs, so the policy is an allow-list, not a proof. The benefit: hostile text in a log or README can at worst produce a bad plan, and the same path serves `ask`, `default` and `auto`.
5. **A fresh inquiry per attempt from a case file, no session resume by the Operator.** Costs some re-reading, gains agent replaceability and simple recovery. Repository context is not re-read, because `inquire` reuses the Leader's session.
6. **Bias toward assuming over asking.** Fewer interruptions, with assumptions always shown. The risk is a wrong assumption; the mitigation is that they are listed in the start message and cheap to correct. The dial ([§4](#4-who-owns-what)) extends this to decisions: cheap-to-undo choices are proceeded on, not waited for.
7. **Cross-component sequencing waits for "Integrated".** Slower than parallel guesses, but correct, because task branches start from the base.
8. **Judge optional, off by default.** Typed escalations ([R2](#required-shikiru-changes)) cover triage, so the first release has no hosted decision dependency. The interface stays, and Jev or Laya is enabled when replays justify it.
9. **Templated reports.** Less eloquent, but they work while every model is down and cannot overstate status.
10. **One instance per product, not one multi-product service.** A bot, journal and service per product, plus a lock file and a claim check shared on the host. Failures and upgrades stay isolated, and the portfolio logic never exists. In return, products do not see each other's quota or load ([§3.1](#31-one-instance-per-product)).
11. **Autonomy grows with evidence.** Classes start low and rise only after repeated unchanged acceptance, and fall at once on a veto. The PO gets slower relief at first, in exchange for no unearned autonomy. The risk is a misclassified decision (a product choice labeled technical); a low starting level, announcements before applying, and immediate demotion bound it.
12. **Model use goes through Shikiru.** One ledger for usage, quota and billing, one set of ladders and plugins, no second agent runtime, and repository questions reach agents that already hold the context. In return, the Operator's reasoning is only as available as Shikiru: if Shikiru itself is broken (a bad upgrade), reasoning stops together with development, monitoring and messaging continue, and the incident is `needs_owner`. Shikiru also gains a read-only inquiry path beside tasks.

**Known limits.** A lost Telegram acknowledgement can produce a duplicate message. Commands across several projects are not atomic, so partial effects are reported and reconciled. The service is one host, not highly available. "Released" is only as good as a configured probe, and is not reported without one.

**Later, only if needed:** mirroring individual decisions into repository docs (the charter is already there), a second channel, email or chat contact for the technical person, release probes, a quota file shared between instances, multiple hosts (each needing its own host module and lock), cross-host MCP over SSH. MCP Tasks and ACP adapters only if they remove code, because neither supplies idempotency, durable state or equal agent capabilities.

## 13. Proving it works

Failure-injection runs against two small real projects, and replays of historical Shikiru escalations through triage:

* A missing system library mid-task. At `ask` the PO or contact sees one fix decision and one tap resolves it; at `auto` it is repaired, verified and resumed with no message.
* A first repair that fails, then a different strategy or agent that succeeds; and one with no solution, which ends in a clear `needs_owner` report.
* An ambiguous goal; a goal with no material question; an infeasible one.
* Two channels answering the same decision at once, and a late stale answer.
* Quota exhaustion of the inquiry ladder and of Shikiru's development ladders, with and without metered authority.
* Kill the Operator mid-install, mid-submit and mid-send; reboot the host.
* One component blocked while another completes.
* A class promoted after five unchanged acceptances, then demoted again by one veto.
* Three goals at once with `max_active_goals` of 2: the third queues and starts when a slot frees.

**Measures, always on from Phase A and not only in drills.** They come from journal timestamps and Shikiru's own task times, and `/flow` and the Monday digest line show them.

| Measure | Why |
|---|---|
| **PO wait time**: decision created to answered or defaulted, and the task-hours blocked meanwhile. This is the primary measure | It is the idle time the Operator exists to remove. Whether it dominates elapsed time is assumption 1 of [§16.2](#162-does-it-boost-development), and the dogfooding baseline will show it |
| **Lead time per goal**: accepted to Integrated, and to PO-accepted | Shows whether development actually got faster |
| **Autonomy share**: decisions and repairs by level, and the recommendation acceptance rate | Shows whether the dial is earning its promotions |
| **Rework rate**: follow-up goals after *Change*, and tasks created after Verified | Speed without correctness is not speed |
| **Time in incidents**, **avoidable PO interruptions**, **missed product decisions**, **duplicated effects**, and **claims of a level the Operator could not prove** | The safety side of the same picture |

The first weeks of dogfooding ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)) provide the baseline to compare against.

## 14. Review: what the scenarios missed

The nine scenarios cover the core loop. Re-reading them as the PO, these gaps remain. Each item is judged against KISS/YAGNI: **MVP** means it ships in the first version because it is cheap and removes real PO work; **Later** means it is designed for but not built until its trigger occurs.

### Gaps in the PO flow

| # | Gap | Behavior | Verdict |
|---|---|---|---|
| 1 | **Goal control from chat.** Scenarios cover starting and answering, not steering. "Do the export before the dashboard", "pause that", "cancel the export", "also include tax" | The `change to a goal` intent maps to `prioritize_task`, `pause_task`, `cancel_task` (always confirmed, since cancelling is a delivery change) or, for an amendment, a new brief version delivered with `send_message` to the Leader or as a follow-up task. The goal's assumptions list is updated and shown | **MVP**, nearly free |
| 2 | **"Is it what I asked for?"** After Integrated the PO must still find out alone whether it works, and acceptance is now the PO's main remaining work | On Integrated, one `inquire` per component, which already holds the diff and reports, compares the brief's acceptance criteria with the verdict, tests and diff facts and sends an **acceptance note** in product language: what the customer can now do, which criteria are evidenced (test results, and screenshots where a verification step produced them), and which are *not checked*. Criteria marked "PO check" come with a **try it** link from the component's optional `preview` command, declared in its `project_info` ([R5](#required-shikiru-changes)); it starts or deploys the integrated build and prints an address. The note ends with **Accept** and **Change** | **MVP**. A component without a `preview` command gets the note only |
| 3 | **Health probes.** Failures are handled after they burn tasks. A red base branch or an agent CLI that auto-updated overnight (agent CLIs update themselves) costs a whole review loop each | The project's declared `health` command (default: Shikiru's `doctor`, plus its `test` command if declared, both from `project_info`) runs before a goal is submitted, after every Integrated item, and nightly together with `doctor --probe`, a disk check and agent login checks. A red result opens an incident *before* work depends on it | **MVP** |
| 4 | **Away mode.** "Silence is never approval" is right but stalls work whenever the PO is asleep or traveling | A modifier of the dial ([§4](#4-who-owns-what)). The PO says "away until Monday" (or taps a button). While away, a reversible, costless decision is treated as `default` with a 4 h veto window and reported on return as "applied your default, here is how to change it". Cost, scope removal, irreversible and access decisions always wait. Options carry `reversible` and `cost` flags from the reasoner | **MVP**: one flag |
| 5 | **Standing rules.** The PO answers the same kind of question repeatedly | Covered at class level by the dial's promotion ([§4](#4-who-owns-what)) and by the charter's precedent amendments. Per-topic rules are not built | Per-topic rules **Later**: the same topic asked three times inside an already promoted class |
| 6 | **Release.** Production release is neither tracked nor performed | An Integrated goal produces one decision, "Release now?", and on yes runs the component's declared `release` command, then its `release_probe`, and only then may reports say "released". It needs its own authority, because it has a production blast radius | **Later**: trigger is the PO wanting releases tracked |
| 7 | **Bug reports with screenshots.** The most common PO input after goals | Attachments are stored and their paths are passed in the brief. Intake first tries to reproduce read-only and attaches the finding. Starts as a goal subtype, not a new pipeline | **Later**: trigger is the first real bug report |
| 8 | **Cost and ETA per goal.** The PO asks "how much is this using?" and "when will it be done?" | Usage is a digest line (MVP, covered above). ETA is a range from the median cycle time of recent similar tasks, queue length and known quota resets | Usage **MVP**. ETA **Later** |
| 9 | **Recurring causes.** The same fingerprint fixed again and again on the host | Two occurrences within 14 days submit a Shikiru task that makes the setup reproducible (bootstrap script or a documented prerequisite), as a technical, decision-free task under class `prevention` (scenario [3](#3-the-first-repair-does-not-work)) | **MVP**, in Phase C |
| 10 | **Technical lead answers** (see the fourth triage route in [§6](#6-the-control-loop)) | Explicit route under class `decision.technical`, so design choices never reach the PO | **MVP** |
| 11 | **Product charter** | Written once in a 15-minute onboarding chat; answers questions before they are asked ([§4](#4-who-owns-what)) | **MVP** |
| 12 | **Goal queue and a limit on active goals.** Handing over several goals at once stretches lead time and exhausts quota | `max_active_goals` (default 2) and a priority order the PO can change (scenario [1](#1-the-po-describes-a-goal)). It queues goals, not tasks; Shikiru still schedules tasks | **MVP**: one number and one state |
| 13 | **Flow measurement.** "Faster" cannot be checked otherwise | PO wait time, lead time, autonomy share and rework rate ([§13](#13-proving-it-works)) | **MVP**: timestamps already exist |

### Risks found in the design itself

* **Untrusted text steering a repair.** Logs, web pages and package READMEs can carry hostile instructions. No Operator agent holds host rights, so the worst outcome is a bad plan. The step policy checks it (allow-listed executables and sources, no pipe-to-shell), a human sees it at the `ask` level, and a probe verifies the result. Research stays in read-only inquiries that return text only, plan execution carries no PO or Telegram credentials, and package-manager state is checked before and after.
* **Silent Operator failure.** The service restarts itself, but if it cannot start nobody is told. The external heartbeat from scenario [8](#8-the-computer-or-the-operator-restarts) should be a documented recommended default, not an afterthought.
* **A misclassified decision.** If a product choice is labeled technical, the Operator decides something that was the PO's. Bounds: classes start low, `default` announces before applying, a veto demotes the class at once, and scope, cost, irreversible and access decisions can never leave `ask`. The `category` and `changes` that agents set are logged with each decision for replay, and the Judge can be enabled if they prove unreliable.

## 15. Implementation effort

### Basis

Estimates are anchored on how Shikiru itself was built with AI agents (git and `tasks/plan.md`), not on guesses:

| Fact | Value |
|---|---|
| Shikiru core without UI (engine, domain, store, operator services, agents, plugins, CLI, MCP) | about 30k lines of source |
| Whole of Shikiru | 56k source and 67k test lines; 1,023 commits; 305 tasks in 36 days |
| Core delivered | about 95 tasks in about 14 days |
| Rework after "feature complete" | four remediation phases (7, 7b, 7c, 7d), about 40 tasks on top of about 55 feature tasks, so budget **0.5–0.7 tasks of rework per feature task** |
| Test code ratio | about 1.2 lines of test per line of source |

Caveat on the anchor: Shikiru's build record shows what agents did on Shikiru, not what Shikiru does on unfamiliar real work (see [§16](#16-assessment-can-shikiru-build-it-and-does-it-boost-development)). Add a **20–30% contingency** for Shikiru defects found while using it for the first time on real work; the pessimistic case in the table below already assumes it.

Shikiri-yaku needs no TUI (the 16k-line dashboard and 3k-line UI layer were the largest and most review-heavy part of Shikiru) and delegates the entire development workflow. It does touch real external systems (Telegram, real agents, package managers, a host reboot), which can be faked less than Shikiru's own internals, so I expect **lower throughput per task**: about 4–6 tasks per day instead of Shikiru's 7.

### Scope: what the estimate covers

Included: scenarios [1](#1-the-po-describes-a-goal)–9 with Telegram only, the MVP items of [§14](#14-review-what-the-scenarios-missed) (including the charter, goal queue, autonomy dial and flow measures), macOS first. The Judge interface exists but is off.

**Cut under YAGNI, with the trigger that brings each back:**

| Cut | Trigger |
|---|---|
| Judge backends (Jev and Laya): only the interface is built | Replays show agents mislabeling `category` or `changes` often enough to matter, or a requirement for no data egress |
| Any channel besides Telegram. Scenario [9](#9-several-channels) is proven with two Telegram chats as separate destinations, because the logic is the same | A real second channel |
| Privileged `yaku-admin` helper. On macOS, Homebrew and user toolchains need no sudo. The helper is built when the first Linux or `sudo` host appears | First Linux host or a package that needs admin rights |
| Any Operator-side model adapter. Model access is Shikiru's, and its Claude and Codex plugins already serve both subscription and API login | A rung the plugins cannot serve, which is then a Shikiru plugin task |
| `yaku init` wizard. `yaku doctor` plus a documented `yaku.toml` is enough for one host | A second installation |
| Release command and probe, per-topic standing rules, bug subtype, ETA, hardening tasks ([§14](#14-review-what-the-scenarios-missed) *Later*) | Their listed triggers |

### Work breakdown

One *task* equals one Shikiru task: an Implementer and Leader loop ending in a reviewed, merged change. Line counts are for production code.

| Phase | Delivers | Feature tasks | Source lines | Usable result |
|---|---|---|---|---|
| **0 Shikiru requirements** | R1–R8 in the Shikiru repository ([§9](#required-shikiru-changes)), in two steps: **0a** R1, R2, R6, R7, R8 (before Phase A) and **0b** R3, R4, R5 (before Phase B). Also the test host, Telegram bot and `yaku.toml` skeleton | 19 in Shikiru (about 10 in 0a, 9 in 0b) | about 2.2k in Shikiru | Shikiru offers the interface the Operator is designed against |
| **A Relay** | Journal and migrations, config, service install, Watcher with MCP link, cursors and snapshot reconcile, engine liveness, Telegram in and out, the decision lifecycle with versions (one destination), Shikiru escalations turned into product-term decisions with a recommendation and buttons, `resolve_escalation`, milestone and digest templates, nightly journal backup, optional heartbeat, flow timestamps, `yaku doctor` | 11 | 1.6k | **The PO answers Shikiru's escalations from Telegram and stops watching tmux.** Real escalation data starts to accumulate |
| **B Goals and autonomy** | Intent routing, the product charter and onboarding chat, Intake with the question gate and definition of ready, goal queue and limit, goal items and origins, routing by cards and per-component assessment, held submission and release for cross-component order, charter commit through a Shikiru task, multiple destinations and conflict handling, goal control, acceptance note with Accept/Change and minimal preview, the autonomy dial (policies, promotion, demotion, away mode), the starred-option route for technical choices, flow measures and `/flow` | 16 | 2.4k | Scenarios [1](#1-the-po-describes-a-goal), [4](#4-a-real-product-decision), [6](#6-the-po-wants-to-know-what-is-happening), [9](#9-several-channels). The PO can drive real work, and routine choices stop reaching the PO |
| **C Repair** | Complete rule triage, case manager and attempt ladder, `inquire`-based investigation returning a plan and probe, step policy and plan execution in the Host module with the lock, fix decisions and graduation along the dial, health probes, access decisions and the technical contact, the prevention task | 9 | 1.8k | Scenarios [2](#2-development-hits-a-technical-problem), [3](#3-the-first-repair-does-not-work), [5](#5-access-or-authority-is-missing), [7](#7-an-agent-becomes-unavailable) |
| **D Hardening** | Ops reconcile for restart, orphan handling, engine auto-start rules, failure-injection drills ([§13](#13-proving-it-works)), security review of the plan and step-policy path, replay of old escalations through triage | 7, plus rework | 0.4k | Scenario [8](#8-the-computer-or-the-operator-restarts) and the proof of [§13](#13-proving-it-works) |
| **Total** | | **about 62 feature tasks (43 in the Operator, 19 in Shikiru)** | **about 8.4k** (6.2k Operator, 2.2k Shikiru; tests about 10k) | |
| **Rework allowance** | Review and remediation rounds | **30–42 tasks** | | |

That is roughly a quarter of Shikiru's non-UI size, and about two thirds of the 95 tasks Shikiru's core took (those 95 already include its remediation) before rework. About a third of the work is in Shikiru, which is where it reduces the Operator's size and risk.

### Time

| Measure | Estimate | Notes |
|---|---|---|
| **MVP**: Phases 0, A, B and the retry, engine-recovery and health-probe slice of C | **3–4 calendar weeks** | About 49 tasks plus rework (about 78 in all), usable for real goals with manual help on technical problems. The Phase A relay is usable after 0a and A, about 21 tasks, so after roughly two weeks (low confidence) |
| **v1**: all phases and the drills of [§13](#13-proving-it-works) | **4–7 calendar weeks**, plan for 6 | About 92–104 tasks including rework at 4–6 a day is 15–26 working days, then a soak week on a real project. The Shikiru tasks touch engine-critical code and need human review, which is why the plan sits in the upper half |
| Optimistic / pessimistic | 4 weeks / 9 weeks | Pessimistic if real-agent or Telegram quirks need redesign, or quota windows throttle the build |
| **Human time** | About 1–2 hours a day, 50–70 hours in total | Reviewing the plan and escalations, answering decisions, running drills. About one day goes to setup: bot, test host, a sandbox project pair, a 15-minute charter chat |
| **Agent compute** | Not estimated | I have no usage figures to extrapolate from. Measure tokens and quota with Shikiru's usage view after Phase A and extrapolate. Expect it to be the limiting factor if the build shares a subscription with other work |

Confidence is medium for the task count and low for calendar time.

### What moves the estimate

* **Down:** reusing Shikiru's agent plugins, ladders and usage ledger through R4, so the Operator has no agent runtime to build; keeping the first channel and platform singular and the Judge off; building Phase B before C so the PO benefits early; running the later phases through the Operator itself once it can submit and report.
* **Up:** R1 and R4 touch Shikiru's engine-critical paths (store migrations, command replay, agent turns), where human review is required ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru), rule 5) and where defects cost the most; adding a Linux host (the helper, a second package manager, a second service manager); any scope creep past [§14](#14-review-what-the-scenarios-missed)'s MVP column; and tuning the dial's classification and promotion rules, which needs real decision history and cannot be sped up with more agents.
* **The biggest risk to the schedule is not code volume. It is the number of real-world integration failure modes (Telegram delivery, agent login expiry, package managers) that only drills reveal.** Budget Phase D and the soak week accordingly rather than trimming them.

### Order of work

Build in the order A, B, C, D so each phase ends with something the PO can use: first a relay that lets the PO answer Shikiru's escalations from Telegram in product terms (this alone ends tmux-watching and starts collecting the real escalation data the decision gate needs), then a goal-driven Operator with the charter and the dial, then a repairing one, then a trustworthy one. Do the Shikiru requirements first, in two steps: 0a (R1, R2, R6, R7, R8) before Phase A, and 0b (R3, R4, R5) before Phase B, since Phases B and C depend on them, and keep each Operator task small enough to pass Shikiru's own review loop in one or two iterations.

## 16. Assessment: can Shikiru build it, and does it boost development?

**Key fact:** Shikiru has no track record on real product work yet. Nothing below is measured on real work. Every figure about benefit is an assumption to be tested.

### 16.1 Can it be implemented with Shikiru?

**Verdict: feasible and a sensible first real trial, but with a fallback.** This host has what Shikiru needs: Shikiru 0.1.0, Python 3.14, tmux 3.7c, git 2.55, uv, `claude` and `codex` CLIs, `gh` and Docker. The `shikiri-yaku` repository exists with an initial commit; it still needs the plan, `AGENTS.md` and `shikiru init`.

**Why it fits**
* The work is specification-driven: a design, a reviewed `tasks/plan.md` with acceptance criteria, and a Leader/Implementer loop. This document is that plan's input. (Shikiru's own repository was built by agents in that style, with plan and todo trackers; there is no `.shikiru` directory in it, so I cannot say it was built *by* Shikiru.)
* The Operator is small and non-visual. It has no TUI, which was most of Shikiru's review cost.
* Shikiru's foundations are reusable: the quality gate and lint boundaries, `AGENTS.md` conventions, `SubprocessRunner`, `Clock`, the SQLite pattern and the agent contracts.
* The best test bed already exists: a real Shikiru engine running its **fake agent** gives the Operator a deterministic Shikiru to watch, escalate and recover, with no model cost. This is also a safer way to find Shikiru defects than real work.

**What the new fact changes.** Building the Operator would be Shikiru's *first real project*. Expect to meet Shikiru defects, stalled tasks and awkward escalations during the build. That is valuable feedback, and it is also the main schedule risk. So:
* Keep a **manual fallback** for any task Shikiru cannot finish after one retry: run the same agent directly on the same branch, then continue with Shikiru. Do not let a Shikiru defect block the Operator.
* Treat the first two or three tasks as a **pilot**, reviewed line by line, before releasing the rest of the plan.
* Record every Shikiru problem met during the build as an input to the Operator design (it is, for free, real incident data).

**Conditions**

| # | Condition | Why |
|---|---|---|
| 1 | Prepare the repo first: one commit with the plan and `AGENTS.md`, then `shikiru init`, with `project.trackers` set to `tasks/plan.md` and `tasks/todo.md` | Shikiru needs a git root with a commit. The first plan is human-reviewed |
| 2 | Keep live externals out of the agent loop. Telegram and package-manager tests are a human-launched smoke script, not part of `poe check` | Agents run with your privileges and environment, and Codex's `workspace-write` sandbox may block network access, a dev server or a browser. Network and host changes would fail or leak, and the bot token must not be in the environment |
| 3 | A disposable host (Docker image or VM) for the repair path | Testing "install a system package and recover" on your machine is unsafe; Shikiru's isolation is a convention, not a sandbox |
| 4 | Pin Shikiru while building with it. Develop R1–R8 on a branch of the Shikiru repo and upgrade the build host deliberately | Entry points are written at install time; a moving engine under a running build is avoidable risk |
| 5 | Small tasks: about 300 lines of change each, with testable acceptance criteria | Large or vague tasks cost review rounds; small, checkable ones keep Shikiru's review loop short (assumption, confirm in the pilot) |
| 6 | Pilot first and keep the manual fallback (above) | Shikiru is unproven on real work |

Phase 0 runs in two repositories (Shikiru: R1–R8; Operator: scaffold copied from Shikiru's gate). Phases A to D follow in dependency order with `integration = "merge"` and `max_parallel = 2`. From Phase A on, the relay lets the PO answer the build's own Shikiru escalations from Telegram, and from Phase B the Operator can supervise the rest of its own build. That is a worthwhile test but must not be the only one.

### 16.2 Does it boost development?

**Honest answer: the mechanism is clear, but nothing has been measured.** Shikiru has not been used on real product work, so the size of the boost is a set of assumptions, not a result. The first weeks of dogfooding ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)) are meant to confirm or refute them.

| # | Assumption | How it is checked |
|---|---|---|
| 1 | Elapsed time in Shikiru-driven development is dominated by waiting for a person (an escalation left unanswered for hours, overnight, or while the PO is busy), not by agent working time | PO wait time and the task-hours blocked meanwhile, against lead time ([§13](#13-proving-it-works)) |
| 2 | A large share of escalations is routine: environment gaps (a missing tool, a dev server that cannot start in the sandbox, no browser for UI checks), integration blocks that a plain retry clears, and technical choices the brief and charter already answer. The rest are genuine product decisions | The escalation corpus from dogfooding, classified by cause ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru), rule 9) |
| 3 | The same environment cause tends to recur until the setup is made reproducible, so fixing it once removes a whole series of interruptions | Recurrence counts per fingerprint |
| 4 | Real work adds genuine product questions, scope disagreements and review rounds, so the avoidable share of interruptions is smaller than in a purely technical exercise. No target figure is promised | Autonomy share and the recommendation acceptance rate ([§13](#13-proving-it-works)) |
| 5 | The Operator removes waiting. It does not make agents write better code or lower their cost | Rework rate and usage ([§13](#13-proving-it-works)) |

**The boost is capped by Shikiru.** If Shikiru's own core loop proves unreliable on real work, the Operator only supervises that unreliability, more quietly.

**Costs the Operator adds:** inquiry usage for assessment and investigation, one more service to keep running, and new failure modes, a wrong repair plan and a misclassified decision. The attempt budgets, the pre-declared verification probe and the reporting of every action bound that.

### 16.3 Recommendation, adjusted for an unproven Shikiru

Do not commit to the whole Operator on the strength of assumptions. Stage it so evidence arrives before the expensive part is built, which is also what YAGNI asks.

1. **Stage 1, dogfooding ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)):** build the Operator's Phase 0 and A with Shikiru, and fix the Shikiru defects that block it with a second Shikiru instance in the Shikiru repo. Record escalations, waits and your commands. This is the first honest baseline.
2. **Stage 2, MVP Phases A and B:** the relay, goal intake, charter, decision flow and autonomy dial. These are useful whatever the escalation mix turns out to be, since they remove the need to watch tmux and decompose goals, and the dial removes waiting for the PO.
3. **Stage 3, decision gate:** replay the real escalations from Stages 1 and 2 through the Phase A triage rules in shadow mode. **Build the repair path of Phase C beyond retries, engine recovery and health probes only if at least about half of real escalations are technical and routable to a rule or a repair.** If most turn out to be product decisions, the better investment is the decision flow, the dial and the acceptance note, and Phase C stays at routine retries and engine recovery.
4. **Stage 4:** hardening, drills and the soak week, as in [§15](#15-implementation-effort).

Under this staging the Operator is built mostly with Shikiru, which doubles as its first real validation, and the largest and riskiest part (executing repair plans on the host) waits for evidence that it is needed.

### 16.4 Dogfooding protocol: build the Operator with Shikiru, fix Shikiru with Shikiru

**Assessment: worthwhile, and the strongest validation available.** It tests Shikiru on real work, produces the real escalation corpus that the [§16.3](#163-recommendation-adjusted-for-an-unproven-shikiru) decision gate and the later check of the agents' typed `category` and `changes` (or an enabled Judge) need, and exercises the Operator's own multi-component case, since the two repositories are two Shikiru projects on one host. The cost is circularity: a Shikiru bug can disable the tool that would fix it. These rules contain that.

| # | Rule | Reason |
|---|---|---|
| 1 | **Two tracks.** A frozen *builder* Shikiru (a tagged, separately installed build, for example `uv tool install --from <tag>`) drives both projects. The Shikiru checkout under change is the *candidate* and is never installed editable on the builder host | An editable install of the checkout whose branches the engine merges would change the running engine under itself |
| 2 | **Promote deliberately.** A candidate becomes the builder only after `poe check` and the repository's release smoke script pass. Back up each project's `state.db` first, and keep the previous tag installed | Migrations only go forward, and the builder owns live state |
| 3 | **Order.** Do R1, R2, R6, R7 and R8 first in the Shikiru repo, promote, then start Phase A on the builder and run R3, R4 and R5 in parallel. R1–R8 are planned changes, not fixes, so they do not count toward the cap of rule 4 | The Operator's phases depend on those changes |
| 4 | **Fix only what blocks or distorts the build.** Shikiru problems get a severity. Blockers and majors are fixed in the same week; the rest go to a list. Cap Shikiru-fix work at about 20% of tasks | Without a cap the project becomes Shikiru's backlog (305 tasks and counting) |
| 5 | **Human review on engine-critical paths** (recovery, command replay, integration, store migrations, escalation). Use different agents for leader and implementer than the ones that wrote the change where the ladder allows | Shikiru's own history needed four remediation phases after independent review. Self-review of the engine by the engine's own agents is the weakest point |
| 6 | **Manual fallback stays.** A task Shikiru cannot finish after one retry is run directly with the agent on the same branch | A broken builder must not block its own repair |
| 7 | **Coordinate quota and resources.** Run the two engines with `max_parallel` of 1 each (or 2 and 1), and watch for both hitting the same provider limit | Each engine keeps its own quota blocks, so two engines on one subscription can exhaust it together. Within one product the Operator at least sees both engines' quota blocks (scenario [7](#7-an-agent-becomes-unavailable)); note any collision as evidence |
| 8 | **Check test isolation in the pilot.** The first Shikiru-fix task must show that `poe check` (it spawns tmux and engines on private sockets) runs cleanly inside a task worktree beside a live engine | It is untested; discover it on a small task |
| 9 | **Log every Shikiru problem** in `docs/dogfooding-log.md`: symptom, evidence (escalation packet path), cause class, workaround, fix task. Keep the packets | This is the replay corpus for triage, the agents' typed fields (or an enabled Judge) and the decision gate |

Once the Operator runs, this same pair is its first product instance: one product, `shikiri-yaku`, with two components (`yaku` and `shikiru`). That also gives the multi-component path a real test.

Two things the pilot must establish before the plan is released: that a Shikiru-fix task can complete end to end under the builder (rule 8), and that a promotion with rollback works on a copy of a real state database (rule 2).
