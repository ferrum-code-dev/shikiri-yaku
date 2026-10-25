# Shikiri-yaku: design

Status: current design, 25 October 2026. No code exists yet. It supersedes the archived initial, v2 and v3 concepts (§17 lists what was kept from them) and builds on what the current Shikiru implementation provides ([Shikiru](https://github.com/ferrum-code-dev/shikiru): its `README.md`, `docs/design.md` and the MCP server). This document is the design, not code.

**How to read it.** §1–§13 are the design: idea, architecture, objects, control loop, the nine scenarios, memory, Shikiru extensions, reasoners and host authority, defaults, trade-offs and the proof plan. §14 reviews it from the PO's side. §15–§16 are the implementation estimate and the feasibility assessment (including the dogfooding protocol). §17 records what the earlier concepts contributed.

## 1. Idea in one paragraph

Shikiri-yaku (the **Operator**) is the one party the Product Owner (PO) talks to. The PO states outcomes and makes product decisions. Shikiru does the development work, one engine per repository. Shikiri-yaku sits between them as a small background service. Toward the PO it takes in goals, asks only the questions that change the product, and reports honestly. Toward Shikiru it behaves like a very attentive human Operator. It submits work, watches events, answers escalations it is entitled to answer and repairs the machine when development stalls. It does not plan, schedule or store the backlog. Its job is to remove idle time (waiting for the PO, for a repair, for a restart), because the pace of the work itself is set by Shikiru and its agents. The measure of success is therefore PO wait time and lead time, not the number of automated actions (§13).

**Deployment unit.** One instance serves exactly one **product**. A product consists of one or more **components**; each component is one Shikiru project (one repository). A PO who manages several products runs several instances, each with its own config, journal, service unit, chat bot and owners (§3.1). In this document "project" means a Shikiru project, that is, a component of the instance's product.

## 2. Principles

1. **Shikiru is the source of truth for development.** Tasks, plans, reports, sessions, escalations and history are read through Shikiru's MCP server. Shikiri-yaku never touches its database and never keeps a copy of the backlog.
2. **Own only what Shikiru cannot own.** That means the conversation with the PO, work that spans projects, the host machine, and the incident memory of this Operator.
3. **Monitoring and communication never depend on a model.** Watching, routing by rule, messaging, digests and recovery are plain code. Models are used to *investigate* and to *interpret*, never to keep the service alive.
4. **Investigate before asking, and proceed before waiting.** The PO is interrupted only for a product decision, or for access and authority that no routine rule covers. "I'm not sure" is a reason to gather evidence, not a reason to ask. A choice that is cheap to reverse is proceeded on and announced rather than waited for, within the autonomy dial the PO controls (§4).
5. **Every side effect is journaled before it happens and verified after.** After any restart the Operator looks at what really happened before it repeats anything.
6. **One decision, one identity, one effect.** A PO decision has a single ID and version across all channels and is applied to Shikiru exactly once.
7. **KISS/YAGNI.** One Python process, one SQLite file, one TOML file, the OS service manager, no queue, no vector store, no agent swarm.
8. **Remove idle time, and measure it.** Shikiru and its agents set the pace of the work. The Operator speeds development by cutting the waiting for the PO, for repairs and for restarts, and it reports PO wait time and lead time.

## 3. Architecture

```mermaid
flowchart LR
  PO([Product Owner]) <--> TG[Telegram] & CH2[later channels]
  TG & CH2 <--> CN[Conversation]
  CN <--> J[(Journal<br/>SQLite)]
  W[Watcher<br/>1 MCP link per project] <--> J
  W <-->|stdio MCP| S1[Shikiru: api]
  W <-->|stdio MCP| S2[Shikiru: web]
  W --> T[Triage<br/>rules + reasoner]
  T --> CN
  T --> C[Case manager]
  C --> R[Reasoner<br/>profile ladder]
  C --> H[Host]
  R -->|agent plugins| AG[Claude / Codex / ...]
  T -.optional.-> JM[Judge: Jev or Laya]
  CN & C --> PL[Policy<br/>autonomy dial]
```

One process, asyncio, one event loop. Modules, not services:

| Module | Responsibility | Uses a model? |
|---|---|---|
| **Conversation** | Channel adapters; inbound dedupe and sender authorization; the `outbox` fan-out and notification budget; decision lifecycle and stale/conflict handling; templates for milestones, digests and on-demand status | Only to interpret free text |
| **Intake** | Turns a PO goal into a *brief per component* using the product charter, with testable acceptance criteria, assumptions and the few material questions; keeps the goal queue; submits to Shikiru; sequences work across components | Reasoner (read-only) |
| **Watcher** | One supervised `shikiru --project P mcp` link per project; durable event cursor; snapshot reconcile; engine liveness; stall watchdog | No |
| **Triage** | Maps every event to a *route* (ignore, report, decision, technical case, wait) by a rule table on Shikiru's typed facts, then by a reasoner classification for the free-text remainder. The Judge only if enabled | Reasoner (Judge optional) |
| **Policy** | The autonomy dial: classes, levels, promotion and demotion from track record (§4) | No |
| **Case manager** | Incidents: fingerprinting, evidence, attempt ladder, budgets, repair plans, verification, parking, closure | Via Reasoner |
| **Reasoner** | Runs read-only intake, explain and investigate agents under a *profile ladder* (agent, model, effort, cost class); tracks blocks and usage | Yes (it *is* the model access) |
| **Host** | Maintenance lock, step policy and plan execution, privileged helper, probes (disk, tools, `shikiru doctor`), engine start/stop | No |
| **Journal** | SQLite store (§8) | No |

**Why a separate service.** The supervisory lifecycle (monitoring, pending decisions, recovery, message delivery) must stay the same when the reasoning agent changes, runs out of quota or crashes. Tying it to one agent runtime would make exactly the failures in scenarios 7 and 8 fatal.

### 3.1 One instance per product

An instance is a directory (`yaku.toml`, `journal.db`, transcripts), a service unit named after the product, and one bot token. There is no registry of products and no portfolio layer.

| Per instance (isolated) | Shared on the host (small and explicit) |
|---|---|
| Config, owners, destinations, journal, goals, decisions, digests | **Maintenance lock**: one lock file in a host-wide state directory, so two instances never change the environment at the same time. It also makes "one repair plan executing at a time" a host-wide rule |
| Telegram bot and chat: the chat itself tells the PO which product is speaking | **Privileged helper** `yaku-admin` and its policy file, when built |
| Instance lock: one process per product directory | **Component claims**: a claim file per repository path, so two instances cannot supervise the same Shikiru project. Start refuses with a clear message |
| Reasoner profiles, blocks and spend limits | Machine capacity and agent subscriptions (see below) |

* **A bot per product.** Telegram allows one long-polling consumer per token, so two instances sharing a token would fight over updates. A bot takes a minute to create and removes all routing logic. Other channels work the same way: one chat or channel per instance.
* **What disappears.** Cross-product digests, "which product?" disambiguation, cross-product sequencing and per-product recipient mapping. A fault, upgrade, rollback or removal of one product cannot touch another.
* **What the isolation costs.** Instances do not see each other's quota blocks, so two products on one subscription each find out about exhaustion on their own, and their Shikiru engines can together exceed the machine. Defaults: a small `max_parallel` per engine, and the host-wide lock for repairs. A shared block file is *Later*, triggered by a second product actually hitting this.
* A combined view across products is outside the scope (YAGNI). The PO simply has one chat per product.

## 4. Who owns what

| Subject | Owner | Operator's access |
|---|---|---|
| Backlog, plans, task state, reports, sessions, review loop, integration, model ladders, quota waits | **Shikiru** | MCP queries and commands only |
| Product knowledge: code, `CLAUDE.md`/`AGENTS.md`, trackers, docs | **Repositories** | Read-only agent sessions in the repo |
| Goals and the link *goal → (project, task id)* | **Operator journal** | Own table; references only |
| Product charter (the principles the PO has set) | **Operator journal** | Own table; versioned |
| PO decisions and everything said to or by the PO | **Operator journal** | Own tables |
| Incidents, attempts, learned fixes, autonomy levels | **Operator journal** | Own tables |
| Credentials, agent logins | **Host/agent-native stores** | Never in the journal or in chat |

Because the PO's past decisions and the charter live in the journal, intake and investigation reasoners receive the relevant ones as context. Mirroring them into repo docs is a later option (§12).

### Progress vocabulary

All reports use four levels, derived from Shikiru state and never inferred:

| Level | Meaning | Shikiru evidence |
|---|---|---|
| **Implemented** | Code written on the task branch | Implementer turn reported `completed` |
| **Verified** | Reviewed and accepted | Leader `verdict` = accept |
| **Integrated** | In the base branch (or the PR flow Shikiru reports) | Task `done` |
| **Released** | In production | Not known to Shikiru. Reported only if a per-component `release_probe` is configured (*Later*, §14). Until then reports say "release not tracked" |

A goal's level is the lowest level among its items. Progress is shown as items per level, never as a percentage of a changing task count. **Accepted** is not a progress level but the PO's own sign-off on a goal (scenario 6); it never blocks dependent work. Until a release probe exists (*Later*, §14), goals are shown as Integrated with "release not tracked", and reports never say "released".

### Autonomy: the contract and the dial

**The contract**, agreed once at setup, answers "when will you bother me?".

| The Operator handles it alone | The PO (or the technical contact) is asked |
|---|---|
| Retrying work, restarting registered services and engines; installing development packages and repairing configuration from trusted sources (at once on a dedicated host, on a workstation after one-tap approvals until the dial promotes it) | A change to customer-visible behavior, scope, quality, priority or delivery date |
| Diagnosing builds, tests and environment faults; researching errors; verifying repairs | Spending outside the standing budget, including a switch from subscription to metered use |
| Technical questions the brief, the charter and earlier decisions answer, and equivalent implementation choices (announced with a veto window until the dial promotes them) | Credentials, MFA, admin rights or accounts the host does not have (to the technical contact if configured) |
| Switching to an allowed agent profile within the cost class; waiting for quota resets | A technical problem still unsolved when the attempt or time budget is spent, stated in product terms |

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
* **Where the classification comes from.** E2's `category` (§9) and per-option `reversible` and `cost` flags set by the reasoner, recorded with each decision. A misclassified decision is the dial's main risk (§14), which is why classes start low and demotion is immediate.
* Levels live in the journal (`policies`). Config sets the starting levels and the ceilings.

### Product charter

A one-page, versioned document owned by the Operator and kept in the journal. It is written once in a 15-minute onboarding chat: the Operator reads the repositories, drafts answers to about ten questions with a recommended answer each, and the PO taps "ok" or corrects. Content: who the users are, the quality bar, hard constraints (budget, compliance, stack), non-goals, default technology choices, test policy, deployment expectations, and how cautious to be.

It does three jobs. Intake treats anything the charter answers as an assumption, not a question. The technical-lead route (§6) answers Leader questions it covers. And any later decision flagged as a precedent proposes a one-line amendment ("add to the charter?"). Most questions from the Leader and from Intake come from missing principles, so this is the cheapest way to cut them. Each brief carries the relevant extract, so Shikiru's Leader sees it too.

## 5. Core objects

Small, durable, and all in the journal. IDs: `G-n` goal, `D-n` decision, `I-n` incident. Project work is addressed as `api/T-12`.

* **Goal**: PO text, state (`clarifying → queued → active → done | cancelled`; `done` means every item is Integrated, and the PO's sign-off is a separate `accepted` mark), priority, brief with acceptance criteria each marked *checked by* (automated test, command, or PO), assumptions, and its *items*: `(project, task_id, after_item?, marker)`. The marker, e.g. `[yaku G-4.2]`, is embedded in the Shikiru task request so a lost receipt can be reconciled by `list_tasks`.
* **Decision**: `kind` (`product | technical | access | spend | fix`), question in product terms, options with consequences and `reversible` and `cost` flags, recommendation, `recipient` (the owner or the technical contact), `state` (`open → answered → applied`, or `stale`, `withdrawn`), `version`, a veto deadline for `default` items, a reference (Shikiru escalation `E-n` or an Operator-originated block), the policy class and level it was handled at, who decided (`po | contact | default | auto`), the verification probe for `access` and `fix`, and the audit trail of who answered through which channel.
* **Incident**: `fingerprint`, scope (project/task/host), class, state (`investigating → repairing → verifying → watching → resolved`, or `parked`, or `needs_owner`), budget used, attempts.
* **Attempt**: approach, hypothesis, evidence refs, the repair plan (ordered steps) and its execution summary with transcript path, verification probe and result, outcome (`fixed | no_effect | worse | inconclusive`), and "what is different from the previous attempt".
* **Policy**: `(class, level, ceiling, streak, last change)`, one row per autonomy class (§4).
* **Charter**: the versioned product principles (§4).
* **Op**: the intent journal. Every command to Shikiru and every host mutation is an Op: `intended → issued → confirmed | failed`, with the key or marker and the verification condition (scenario 8).

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
1. *Rule table* on Shikiru's own typed facts: escalation `reason` (`agent_auth`, `quota_exhausted`, `integration_blocked`, `engine_error`, …), event kind (`engine_fault`, `quota_blocked`, `task_done`, …) and `needs_input`. Most events end here.
2. *Reasoner classification* for the ambiguous remainder, chiefly `decision_required` when E2's category is absent (Leader escalations mix product questions and "missing access" in free text), `agent_error` and `engine_error` text. It returns a structured verdict over a compact, redacted digest: route class, whether resolving this changes behavior, scope, quality, priority, cost or delivery, a reason, and a `reversible` flag. Low certainty means *more evidence*, never a PO question. The optional Judge (§10) can take over this step if replays show a need.
3. *Investigate* when still unclear. The case manager gathers evidence and may ask the reasoner to classify.

A fourth route covers Leader questions that are neither product nor environment (E2 category `technical`), such as which of two equivalent implementation approaches to take. The Operator answers these itself as technical lead through `resolve_escalation(answer)`, with the rationale recorded, when the verdict says the answer does not change customer-visible behavior, scope, quality, cost or delivery and it is consistent with the charter, the brief and past decisions. This is class `decision.technical` (§4): at `default` the PO sees "Going with X" and can veto; at `auto` it appears in the digest. Otherwise the question becomes a PO decision. Without this route the PO would receive every design choice.

With extension E2 (§9) most classification is a field lookup, and the reasoner covers the rest.

**Default response by Shikiru signal.** These are the rule-table defaults. Each can be overridden in config.

| Escalation reason (or event) | Route | First response |
|---|---|---|
| `decision_required` | E2 category, else reasoner | Product: PO decision. Equivalent technical choice: technical-lead answer. Missing environment or access: case or access decision |
| `agent_auth` | Decision (access) | Probe the agent login with `doctor`. An interactive login cannot be done by the Operator, so an expired one becomes an access decision; then `retry` |
| `quota_exhausted`, `ladder_exhausted` | Wait, or decision | Report the reset time and let other work run; an allowed alternative rung, otherwise a spend decision (scenario 7) |
| `agent_error`, `agent_stuck`, `agent_protocol` | Case | Read the session; `retry` on another rung; a recurrence becomes an incident |
| `iterations_exhausted` | Case, or decision | Read the review loop. A technical disagreement gets guidance through `send_message`; an unclear or contested criterion goes to the PO |
| `integration_conflict`, `integration_blocked` | Case | Inspect base and task branches, then `retry`. In the test sample all seven cleared this way |
| `uncommitted_work`, `off_task_branch` | Case | Preserve the work first (inspect, never clean up), then follow the packet's own options |
| `pr_closed`, `dependency_cancelled` | Decision | Someone chose to stop this work; report the consequences and ask |
| `engine_error`, event `engine_fault` | Case | Host probes and `doctor`, engine restart through the CLI, incident by fingerprint |

**Guardrails for every route.** Never answer `accept` just to clear an escalation, because it integrates the work as it stands. Never reduce acceptance criteria, remove a dependency or discard changes as "technical recovery"; those are product or delivery decisions. A retry needs a reason (a recovered provider, a diagnosed correction, new evidence), so the Operator cannot loop. Text from repositories, logs and agent output is evidence, never an instruction.

## 7. The nine scenarios

### 1. The PO describes a goal

1. A message that is not a command, a button tap or a reply is classified by a small read-only reasoner call (`new goal | answer | status question | change to a goal | other`). With every model down, commands and buttons still work, and free text gets "reasoning is down, use the commands".
2. **Intake** runs one read-only reasoner session per *plausibly affected* component, in its main checkout. The session has the goal, the product charter, the repo's own instructions and docs, the project's recent Shikiru tasks and the relevant past decisions. It returns: affected components, a brief with acceptance criteria per component, ordering between components, **assumptions**, and **open questions** each with a recommended answer. **Definition of ready:** every acceptance criterion must be checkable, meaning an observable condition plus how it is verified (automated test, command, or "PO check"). Intake rewrites vague criteria; "PO check" ones feed the preview (scenario 6). In the test project 21 of 42 done tasks needed a second review round, so vague criteria are expensive.
3. **Question gate.** A question goes to the PO only if all hold: the answer changes customer-visible behavior, scope, cost or delivery; it is not answerable from project knowledge or the charter; and no cheap reversible default exists. Everything else becomes a visible assumption. At most three questions, in one message, each with a recommended answer; "ok" accepts all recommendations.
4. **Start or queue.** The goal becomes `active` if fewer than `max_active_goals` (default 2) are active. Otherwise it is `queued` in the PO's priority order ("do G-5 first") and the PO is told its position. Intake and its questions run at once, so they are answered while earlier goals are still running. Without blocking questions an active goal is submitted at once, and the Operator tells the PO what it understood, what it assumed and where work started. With blocking questions it submits the unaffected components and holds the rest.
5. **Submission.** One `submit_task` per component carries the *outcome-level brief*, not technical tasks. Shikiru's Leader triages and decomposes (its `plan` mode). Cross-component order (an API before its client) is the Operator's only scheduling duty: the dependent item is submitted when its prerequisite is **Integrated**, because Shikiru task branches start from the base branch. Shikiru's `depends_on` is per project, and Shikiru runs one engine per repository, so multi-repository coordination sits above it and the Operator keeps the link table.

### 2. Development hits a technical problem

Typical sources: an `escalation` with a technical reason, an `engine_fault`, an Implementer blocked on a tool, repeated `iterations_exhausted`, or a task with no events past the stall threshold.

1. Triage opens or **reopens by fingerprint** an incident. The fingerprint is project, reason, step and a normalized error signature (paths, ids and numbers stripped).
2. **Quiesce only if needed.** Additive fixes (installing a missing package) do not stop work. Changes that replace a toolchain version under running tasks do: the Operator pauses the affected tasks, or stops the project's engine (`shikiru stop` acknowledges after the lock is released), and resumes only what it stopped. It confirms from `status` that the work has really stopped before the change starts; an accepted pause command is not enough.
3. **Evidence first**, collected by plain code: the escalation packet and its "what was tried" digest, recent events, `peek_session`, `shikiru --json doctor`, disk and memory, tool versions, the failing command re-run in the task worktree.
4. **Plan, then execute.** Product-code problems are not repaired by the Operator. They return to Shikiru as guidance (`send_message` plus `retry_task`/`resolve_escalation retry`) or as a new Shikiru task. For host problems (missing system library, broken PATH, full disk, stopped service) a read-only `investigate` session returns the diagnosis, a **repair plan** (ordered steps, each marked reversible or not), the **verification probe**, and what differs from earlier attempts. No agent holds host rights. The Host module checks the plan against the step policy (§10), and the plan is applied according to the dial class `repair.*` (§4): at `ask` it goes as a *fix* decision to the technical contact or PO (diagnosis, exact steps, probe, one tap to apply); at `default` it is applied after the veto window; at `auto` at once. The Host module executes it step by step under the maintenance lock.
5. **Verify** with a probe declared *before* acting and executed by the Operator, not self-reported: the failing build or test command passes, then Shikiru resumes. The incident moves to `watching` and closes only after the task passes the failed step or a watch window elapses without recurrence.
6. At level `auto` the PO sees nothing at the time; the repair appears in the next digest, immediately only if it delayed delivery by more than an hour. At `ask` and `default` it was the one-tap decision above, and each unchanged approval counts toward promotion (§4).

### 3. The first repair does not work

* An incident has an **attempt ladder** with a hard budget (5 attempts, 2 hours, and a spend cap if a metered profile is used). Reopening by fingerprint never resets it.
* **Attempts must differ.** A plan is rejected if its `(approach, target)` equals a failed attempt and it cites no new evidence.
* **Strategy escalation:** (a) remembered fix for the same fingerprint, if any; (b) primary profile with the evidence bundle; (c) same profile with a deeper evidence pass (full logs, dependency tree, system journal) and the explicit list of failed attempts; (d) a different agent or model from the ladder; (e) external help via an access decision, or `needs_owner`.
* The case file is re-rendered from the journal for every attempt, so changing agent loses nothing and no native session needs to be portable.
* **Fencing.** Every attempt has a number. The result of an attempt that is no longer current (a slow agent that timed out and was replaced) is discarded and never applied. Before a replacement starts, the previous attempt's uncertain host changes are reconciled.
* Exhausting the budget gives a short owner message in product terms: what is blocked, what was tried, what is needed or which decision unblocks it. It is still not a request that the PO run commands.
* **Fix once.** A fingerprint that recurs within 14 days leads to a decision-free Shikiru task that makes the setup reproducible (a bootstrap script or a documented prerequisite), under class `prevention`. Repairing the same thing repeatedly is the failure mode: in the test sample one environment cause came back five times.

### 4. A real product decision

Sources: a Leader `decision_required` that E2's `category` (or the reasoner) marks as product; an Operator finding that the goal is infeasible under current constraints (for example it needs a paid service or breaks an earlier decision); or a proposed change to scope, quality, priority, cost or delivery.

1. The **Decision** is written in product terms: the issue, 2–3 feasible options with consequences (behavior, effort/time, cost, risk), and a recommendation. Technical detail stays one tap away ("details"). An illustration (invented content):

   > **Shop · invoice export · D-7**
   > The payment provider cannot supply invoices older than 90 days. I checked the API and tried a fallback; neither covers the full requirement.
   > A. Ship export for new invoices now, add history later.
   > B. Keep full history in scope and move delivery while a second data source is added.
   > C. Pause export and start the next feature.
   >
   > Recommended: A, if history can follow separately. New-invoice work continues; history is on hold until you answer.
2. **The dial.** Before asking, Policy looks up the decision's class (§4). `ask`: continue as below. `default`: the message says "Going with A in 15 min unless you object" and the Operator applies the recommendation after the veto window; this is the Operator's own decision, taken under authority the PO granted and announced. `auto`: applied and listed in the digest. Scope, cost, irreversible and access decisions are always `ask`.
3. **Waiting without blocking.** The affected Shikiru task is already `escalated` and frees its slot. Unaffected tasks and other projects continue. For Operator-originated decisions the Operator holds only the affected items.
4. **No material change before the answer.** Shikiru's Leader may change scope and notify afterwards. With extension E1, Leader-created tasks start held and the Operator releases or cancels them after the decision. Without E1 the Operator can only react after the fact by cancelling the new tasks (see the limits in §12).
5. **Applying.** Chosen option → Shikiru command: for an escalation, `resolve_escalation(answer, text)` where the text is generated from the chosen option (agent alternatives never select commands in Shikiru, so the Operator maps them). Silence never approves an `ask` decision. Unanswered `ask` decisions are re-sent as reminders (default after 4 h, then daily).

### 5. Access or authority is missing

* **Standing authority** is the dial's starting position (§4, §10): retries, engine restarts and switching to *allowed* agent profiles within the cost class never prompt, and on a dedicated host neither do user-level repairs. Other repairs start at `ask` and are promoted by track record.
* When something outside it is genuinely required, the Operator creates an **access decision** (kind `access`) with an exact request (what, where, why, the verification probe), alternatives (a mock or stub, another provider, a narrower scope, waiting) and the delivery impact.
* **Routing:** if a technical contact is configured and `authorized_requests` covers this kind, the decision goes to them as recipient through their own channel, the PO is told the impact ("web is waiting for X from the technical contact; api continues"), and the contact's "done" is accepted only after the probe passes. Otherwise the PO gets a short decision: authorize, choose an alternative, or accept the delay.
* **Secrets never travel through chat.** The request tells the contact where on the host to put them. An inbound message that looks like a secret is rejected with instructions and, where the channel allows, deleted.

### 6. The PO wants to know what is happening

* **Milestones** (immediate, templated from queried state, within a notification budget of 3 a day beyond decisions and access requests; the rest are folded into the digest): goal accepted and started; each component reaching a new level; goal Integrated; a decision needed; an access request; a delivery slip caused by a blocker older than an hour.
* **One digest** at 09:00 on weekdays, skipped if nothing changed. Sections: *Completed outcomes* with levels, *In progress*, *Queued*, *Blocked*, *Technical problems resolved* (one line each), *Decisions the Operator took under the dial* (with how to change them), *Decisions needed*, and a one-line *Usage* summary per goal (from `status.usage` and `status.quotas`, with unknown values shown as unknown). On Mondays a *Flow* line adds PO wait time and lead time (§13). Wording comes from templates and live queries, so digests work with every model down.
* **Acceptance.** When a goal is Integrated the PO gets an acceptance note (§14, gap 2) ending in **Accept** and **Change**. Accept stamps the goal as accepted; Change opens a linked follow-up goal. A goal that is not yet accepted never blocks other work, and is reminded once, then weekly.
* **On demand:** `/status`, `/goal G-4`, `/why api/T-12`, `/problems`, `/decisions`, `/flow`, plus free-text questions. Details are fetched live (`get_task`, `peek_session`) and summarized by a read-only reasoner when available, or shown as structured facts when not.
* Reports state the level of every claim. "Done" is never used without a level, and "Released" appears only with a configured probe (§4).

### 7. An agent becomes unavailable

Two different things can fail, and the Operator keeps them apart.

* **Shikiru's own agents** (Leader/Implementer). Shikiru already waits for quota resets, climbs its ladders and escalates `quota_exhausted`/`ladder_exhausted`. The Operator reports the wait with the reset time from `status.quotas` and lets unaffected work run. It intervenes only when Shikiru escalates, and then chooses between an already-configured alternative rung (`set_rung`/`retry`), waiting, or a **spend decision** to the PO.
* **The Operator's own reasoners.** The Reasoner returns Shikiru's normalized failure kinds (`QUOTA` with `resets_at`, `UNAVAILABLE`, `AUTH`, `OTHER`). A quota or auth failure writes a block `(profile, until)`, and the next permitted profile runs with the same case file. If none is permitted the incident is `parked` with a wake-up time. Watching, messaging, digests, rule triage and recovery keep running, so "reasoning is down" is itself reported ("investigation paused until 14:10; monitoring continues").
* **Spending is never switched silently.** Profiles carry a cost class (`subscription` or `metered`). Moving to a more expensive class needs a standing budget (`metered_usd_per_day`, default 0). Otherwise the case becomes a PO decision with "wait for reset" as the recommended option.

### 8. The computer or the Operator restarts

On start the Operator takes a single-instance lock and then, before accepting new work:

1. **Orphans.** Reasoner and host-helper children recorded in `ops` (pid plus start time) are terminated if still alive.
2. **Reconcile every non-final Op by observing, not repeating:**
   * *Submit*: look up the marker in `list_tasks`; submit only if absent.
   * *Resolve/retry/cancel/send_message*: read the escalation or task state; Shikiru's own command replay is idempotent by command id.
   * *Host change*: each plan step is its own Op. Run the Op's verification condition first; if it passes the Op is `confirmed`; if not, the next attempt is told "interrupted, inspect before acting" and package-manager state is checked before any reinstall.
   * *Outbound message*: the Bot API gives no delivery receipt, so an uncertain message is re-sent with its stable reference (`D-12`) to make a duplicate recognizable.
3. **Engines.** `status.engine_up` per project. An engine that is down *without* a recorded `engine_stopped` event died or was killed (host reboot, OOM) and is started with `shikiru start`; a recorded stop is respected. A fault stop (`engine_stopped` with `reason`) is an incident.
4. **Decisions** still `open` or `answered-not-applied` are resumed. Open ones are re-sent only if they were not confirmed delivered.
5. A short "back online after Nm" notice is sent if the outage exceeded 10 minutes. An optional external heartbeat URL covers the case in which the Operator itself cannot report.

### 9. Several channels

* A **destination** is `(channel, chat, role)`. All milestones, digests and decisions are enqueued once per destination in `outbox`, carrying the object's ID and version. Targeted replies (answer to a question) go only to the asking destination.
* A reply arrives as an `inbound` row (channel, message id, sender). The sender must be an authorized owner. The reply names its decision and version (button payload `D12.v2.B`, reply-to mapping, or interpretation of free text).
* **First valid answer wins** through one conditional update, `UPDATE decisions SET state='answered' … WHERE id=? AND version=? AND state='open'`. The winner is enqueued as an Op and applied to Shikiru once.
* A later answer gets a precise reply: *"Already decided: B, via Telegram at 12:03 (applied)"*. If the decision was only answered, not applied, a conflicting reply is refused the same way. Changing an applied decision is a **new** decision with its own consequences.
* A reply to an old version, or to a decision whose facts changed or that Shikiru already resolved, is answered with *stale* plus the current question.
* Free-text answers are mapped to an option by the reasoner (or the Judge if enabled). Anything below the confidence threshold is echoed back for a one-tap confirmation rather than applied.
* After `applied`, every destination's original message is edited to the final state (buttons removed). Channels that cannot edit get a follow-up line.

## 8. What must be remembered between runs

One SQLite file (WAL), written only by the Operator, backed up nightly (§11). Everything else is read from Shikiru.

| Table | Contents | Why it must survive |
|---|---|---|
| `cursors` | Last consumed event id per project, snapshot time | Resume without losing or replaying events |
| `goals`, `items` | PO text, brief, priority and queue position, assumptions, project/task/marker links, announced levels, acceptance | Cross-project sequencing; no repeated milestones |
| `charter`, `policies` | Versioned product principles; autonomy level, ceiling and streak per class | The PO's standing choices and earned autonomy |
| `decisions`, `options` | Kind, recipient, question, options with flags, version, state, veto deadline, policy class and level, who decided where, application Op, probe | Unanswered decisions, exactly-once application, track record |
| `inbound`, `outbox` | Per-destination messages, delivery state, platform message ids | Dedupe, edits, retries per destination |
| `incidents`, `attempts` | Fingerprint, budget, every attempt with evidence and verification | Avoid repeat failures; continue after agent change or restart |
| `ops` | Intent journal for Shikiru commands and host mutations, pids | Safe restart |
| `fixes` | Fingerprint → the repair plan that worked and the conditions it relied on | Fast known-fix path |
| `profiles_state` | Blocks `(profile, until)`; usage and estimated spend only when a metered budget is configured | No silent spending; fallback timing |

Not stored: tasks, plans, reports, transcripts, escalation packets, logs (they stay in Shikiru and are fetched on demand). Retention: closed incidents and delivered messages 90 days; decisions, policies, the charter and fixes indefinitely.

## 9. Shikiru: what is used, what is needed

### Used as it is

| Capability | Used for |
|---|---|
| MCP `submit_task` (priority, `depends_on`, mode, complexity) | Starting component work |
| MCP `wait_for_events` with cursors | Event intake |
| MCP `status`, `list_tasks`, `get_task`, `list_escalations`, `get_escalation`, `peek_session` | Facts, evidence, reporting |
| MCP `resolve_escalation`, `retry_task`, `pause_task`, `resume_task`, `cancel_task`, `set_rung`, `send_message` | Acting on cases and decisions |
| Escalation packets (reason, options with consequences and ★, `needs_input`, "what was tried") | Decision text and incident evidence |
| Idempotent command replay, engine reconcile, durable queue while the engine is down | Restart safety |
| Quota/agent blocks and ladders in `status` | Scenario 7 |
| CLI `start`, `stop`, `doctor --json`, `ls` | Engine lifecycle and host probes (the MCP has no lifecycle tools) |
| `shikiru.agents` contracts and plugins: normalized signals, usage, quota, `FailureKind`, `doctor()`, optional `headless().ask` | Reasoner adapters |

### Extensions requested

| # | Extension | Need | Size |
|---|---|---|---|
| **E1** | Option `orchestration.scope_change = "hold"`: tasks the Leader creates through `new_tasks`/`supersede` start with the existing `paused` hold, and the `scope_change` event lists titles and reason | **Required** for scenario 4's guarantee that no material change happens before the PO's answer. Today Leader scope changes take effect first and are notified afterwards | Small, reuses holds |
| **E2** | A typed `category` (`product`, `technical`, `environment`, `access` or `other`) on Leader escalations and Implementer `blocked` outcomes | Recommended, and with the Judge off by default it is the main source of correct routing. Without it the reasoner classifies | Small: schema field and prompt line |
| **E3** | Client idempotency key on `submit_task`, with receipt lookup | Optional. The embedded marker plus `list_tasks` already covers a lost receipt | Small |
| **E4** | A supported way to run a non-task agent turn: packaged `shikiru.agents` as a stable SDK with a read-only permission profile per call | Recommended. `headless().ask` is schema-only with no tools (Claude `--tools ""`, Codex read-only), and `start_turn` expects task paths. Shikiru's `agents/sdk` offers process and stream tools for plugin authors, not an entry point for a non-task turn. The Operator's agents never need write rights, so one profile is enough. Until then the Operator writes a ~100-line signal consumer over `start_turn` | Medium |
| **E5** | `pause_project` (stop dispatch, keep the engine and tasks) | Optional; `shikiru stop` already quiesces, more coarsely | Small |

Two points need confirming in code before implementation: that the `escalation` event data carries enough to avoid an extra `get_escalation`, and the exact PR-mode meaning of `task_done`. Neither changes the design.

## 10. Reasoners, the optional Judge and host authority

**Reasoner.** A narrow interface: `run(role, case_file, profile) → structured outcome | failure kind`. Roles: `intake`, `explain` and `investigate`. All are read-only, and `investigate` returns a diagnosis, a repair plan and a probe. No agent holds host rights; plans are executed by the Host module (below). A **profile** is `{agent, model, effort, options, cost_class}`. The ladder is configured per role. The first adapter reuses the Shikiru agent plugins, so Claude and Codex work with native subscription *or* API login exactly as Shikiru does, and access stays in the agent's own configuration. A direct-API or local-model adapter needs only the narrow interface. Profiles are tested at startup with a free probe (`doctor`); a blocked profile is never re-probed with inference calls, only with `doctor` or after its reset time. Each run gets a case file, a time limit and a transcript directory, and a **curated environment** containing only what its profile declares. An API key inherited from the shell can therefore never move a subscription profile onto metered billing without anyone noticing.

**Judge (optional, off by default).** The brief asks for decision models where they simplify triage and routing. With E2 and the reasoner's structured classification they do not in the first release, so the Judge is built as an interface only. It adds a hosted dependency, data leaving the host and a calibration effort. The interface stays so that it can be switched on when replays of real escalations show a frequent route that is mislabeled, too slow or too costly through the reasoner. One call, `judge(digest, questions) → choice/noul with probability and confidence`, over a compact redacted digest (headline, reason, tried-lines, option titles) of about 1,000 tokens at most. Both candidates provide the needed primitives (`choice`, `score`, `noul` with calibrated probabilities). The checked facts about the two backends:

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
| Deployment | `uv tool install`; `yaku install-service --config <dir>/yaku.toml` writes a launchd/systemd unit named `yaku.<product>` with restart-on-failure. `yaku doctor` checks channels, links, profiles, helper and compatibility (pinned Shikiru version, MCP tool set, agent CLIs), because those interfaces change under us |
| Config | One `yaku.toml` per product: product name, components (name, path), owners and destinations, timezone, contacts, profiles and ladders, host mode and authority, schedule |
| Instance | One instance = one product; each Shikiru project is a *component* (§3.1) |
| Owners | One responsible owner plus optional delegates, as an allow-list; no voting |
| Telegram | One bot per instance, long polling, an allow-list of user ids, inline buttons, edit-in-place. Updates are saved before the offset advances. Telegram keeps undelivered updates for about 24 hours, so after a longer outage open decisions are re-presented |
| Question gate | Max 3 questions, each with a recommended answer; reversible choices become assumptions |
| Autonomy | Starting levels as in §4; veto window 15 min (4 h while away); promotion offered after 5 consecutive unchanged acceptances and not repeated for 30 days; demotion at once on a veto, reversal or failed probe |
| Goals | `max_active_goals` = 2, the rest queued by priority; the PO's acceptance never blocks other work |
| Notices | At most 3 proactive messages a day besides decisions and access requests; the rest go into the digest |
| Digests | One, weekdays 09:00; none if nothing changed. Mondays add a flow line |
| Decision reminders | After 4 h, then daily |
| Incident budget | 5 attempts, 2 h wall time, no metered spend |
| Stall watchdog | A running task silent for 30 min (above Shikiru's own 20 min stall bound) |
| Watch window after a fix | 30 min or the next completed turn on the task |
| Parallelism | 1 repair plan executing (host-wide), 2 read-only investigations per instance; a small `max_parallel` (1 or 2) per Shikiru engine |
| Event gap | Resync from snapshots; no assumption that history is complete |
| Retention | Closed incidents and delivered messages 90 days |
| Backup | Nightly copy of `journal.db` through SQLite's backup API to a configured path, 7 copies. Restoring is copying it back; the restart reconcile (scenario 8) then runs |
| Heartbeat | An optional external URL pinged every 5 min; recommended in the setup guide, because a service that cannot start cannot report that |

## 12. Consequential trade-offs and limits

1. **Standalone service rather than a plugin in an agent runtime.** More code and one more service, but monitoring, pending decisions and recovery keep their meaning when the agent changes or fails.
2. **MCP only, never the database.** A stable public contract and no schema coupling, at the cost of the queries and long-poll shape Shikiru exposes. Lifecycle (`start`, `doctor`) uses the CLI because the MCP has no tools for it.
3. **A small own journal rather than state in Shikiru.** Delivery receipts, host repairs and PO conversations do not belong in project databases. The price is that the goal→task link table overlaps in spirit with Shikiru's graph. It stays minimal because Shikiru has no concept above a project.
4. **Agents propose, the Host executes.** No Operator agent holds host rights. A repair is a plan of steps checked against a step policy, shown to a human at the `ask` level and verified by a probe. The cost: exploratory trial and error takes several attempts (each attempt's read-only findings feed the next), and plans are shell steps rather than typed verbs, so the policy is an allow-list, not a proof. The benefit: hostile text in a log or README can at worst produce a bad plan, and the same path serves `ask`, `default` and `auto`.
5. **Fresh reasoner sessions from a case file, no session resume.** Costs some re-reading, gains agent replaceability and simple recovery.
6. **Bias toward assuming over asking.** Fewer interruptions, with assumptions always shown. The risk is a wrong assumption; the mitigation is that they are listed in the start message and cheap to correct. The dial (§4) extends this to decisions: cheap-to-undo choices are proceeded on, not waited for.
7. **Cross-component sequencing waits for "Integrated".** Slower than parallel guesses, but correct, because task branches start from the base.
8. **Judge optional, off by default.** E2 and the reasoner's classification cover triage, so the first release has no hosted decision dependency. The interface stays, and Jev or Laya is enabled when replays justify it.
9. **Templated reports.** Less eloquent, but they work while every model is down and cannot overstate status.
10. **One instance per product, not one multi-product service.** A bot, journal and service per product, plus a lock file and a claim check shared on the host. Failures and upgrades stay isolated, and the portfolio logic never exists. In return, products do not see each other's quota or load (§3.1).
11. **Autonomy grows with evidence.** Classes start low and rise only after repeated unchanged acceptance, and fall at once on a veto. The PO gets slower relief at first, in exchange for no unearned autonomy. The risk is a misclassified decision (a product choice labeled technical); a low starting level, announcements before applying, and immediate demotion bound it.

**Known limits.** Without E1, material scope changes by the Leader are discovered after the fact and are undone by cancelling. Without E3, duplicate submission is prevented by marker lookup, not by a server guarantee. A lost Telegram acknowledgement can produce a duplicate message. Commands across several projects are not atomic, so partial effects are reported and reconciled. The service is one host, not highly available. "Released" is only as good as a configured probe, and is not reported without one.

**Later, only if needed:** mirroring decisions into repository docs, a second channel, email or chat contact for the technical person, release probes, a quota file shared between instances, multiple hosts (each needing its own host module and lock), cross-host MCP over SSH. MCP Tasks and ACP adapters only if they remove code, because neither supplies idempotency, durable state or equal agent capabilities.

## 13. Proving it works

Failure-injection runs against two small real projects, and replays of historical Shikiru escalations through triage:

* A missing system library mid-task. At `ask` the PO or contact sees one fix decision and one tap resolves it; at `auto` it is repaired, verified and resumed with no message.
* A first repair that fails, then a different strategy or agent that succeeds; and one with no solution, which ends in a clear `needs_owner` report.
* An ambiguous goal; a goal with no material question; an infeasible one.
* Two channels answering the same decision at once, and a late stale answer.
* Quota exhaustion of the reasoner and of Shikiru's agents, with and without metered authority.
* Kill the Operator mid-install, mid-submit and mid-send; reboot the host.
* One component blocked while another completes.
* A class promoted after five unchanged acceptances, then demoted again by one veto.
* Three goals at once with `max_active_goals` of 2: the third queues and starts when a slot frees.

**Measures, always on from Phase A and not only in drills.** They come from journal timestamps and Shikiru's own task times, and `/flow` and the Monday digest line show them.

| Measure | Why |
|---|---|
| **PO wait time**: decision created to answered or defaulted, and the task-hours blocked meanwhile. This is the primary measure | It is the idle time the Operator exists to remove. The test sample had 631 task-hours of waiting |
| **Lead time per goal**: accepted to Integrated, and to PO-accepted | Shows whether development actually got faster |
| **Autonomy share**: decisions and repairs by level, and the recommendation acceptance rate | Shows whether the dial is earning its promotions |
| **Rework rate**: follow-up goals after *Change*, and tasks created after Verified | Speed without correctness is not speed |
| **Time in incidents**, **avoidable PO interruptions**, **missed product decisions**, **duplicated effects**, and **claims of a level the Operator could not prove** | The safety side of the same picture |

The first weeks of dogfooding (§16.4) provide the baseline to compare against.

## 14. Review: what the scenarios missed

The nine scenarios cover the core loop. Re-reading them as the PO, these gaps remain. Rows 11 to 13 come from a second review focused on speed and PO workload. Each item is judged against KISS/YAGNI: **MVP** means it ships in the first version because it is cheap and removes real PO work; **Later** means it is designed for but not built until its trigger occurs.

### Gaps in the PO flow

| # | Gap | Behavior | Verdict |
|---|---|---|---|
| 1 | **Goal control from chat.** Scenarios cover starting and answering, not steering. "Do the export before the dashboard", "pause that", "cancel the export", "also include tax" | The `change to a goal` intent maps to `prioritize_task`, `pause_task`, `cancel_task` (always confirmed, since cancelling is a delivery change) or, for an amendment, a new brief version delivered with `send_message` to the Leader or as a follow-up task. The goal's assumptions list is updated and shown | **MVP**, nearly free |
| 2 | **"Is it what I asked for?"** After Integrated the PO must still find out alone whether it works, and acceptance is now the PO's main remaining work | On Integrated, a read-only reasoner compares the brief's acceptance criteria with the verdict, tests and diff facts and sends an **acceptance note** in product language: what the customer can now do, which criteria are evidenced (test results, and screenshots where a verification step produced them), and which are *not checked*. Criteria marked "PO check" come with a **try it** link from the component's optional `preview.command` (it starts or deploys the integrated build and prints an address). The note ends with **Accept** and **Change** | **MVP**. A component without `preview.command` gets the note only |
| 3 | **Health probes.** Failures are handled after they burn tasks. A red base branch or an agent CLI that auto-updated overnight (agent CLIs update themselves) costs a whole review loop each | A per-project `health.command` (default: `shikiru doctor`, plus the repo's test command if configured) runs before a goal is submitted, after every Integrated item, and nightly together with `doctor --probe`, a disk check and agent login checks. A red result opens an incident *before* work depends on it | **MVP** |
| 4 | **Away mode.** "Silence is never approval" is right but stalls work whenever the PO is asleep or traveling | A modifier of the dial (§4). The PO says "away until Monday" (or taps a button). While away, a reversible, costless decision is treated as `default` with a 4 h veto window and reported on return as "applied your default, here is how to change it". Cost, scope removal, irreversible and access decisions always wait. Options carry `reversible` and `cost` flags from the reasoner | **MVP**: one flag |
| 5 | **Standing rules.** The PO answers the same kind of question repeatedly | Covered at class level by the dial's promotion (§4) and by the charter's precedent amendments. Per-topic rules are not built | Per-topic rules **Later**: the same topic asked three times inside an already promoted class |
| 6 | **Release.** Production release is neither tracked nor performed | An Integrated goal produces one decision, "Release now?", and on yes runs the component's `release.command`, then its `release_probe`, and only then may reports say "released". It needs its own authority, because it has a production blast radius | **Later**: trigger is the PO wanting releases tracked |
| 7 | **Bug reports with screenshots.** The most common PO input after goals | Attachments are stored and their paths are passed in the brief. Intake first tries to reproduce read-only and attaches the finding. Starts as a goal subtype, not a new pipeline | **Later**: trigger is the first real bug report |
| 8 | **Cost and ETA per goal.** The PO asks "how much is this using?" and "when will it be done?" | Usage is a digest line (MVP, covered above). ETA is a range from the median cycle time of recent similar tasks, queue length and known quota resets | Usage **MVP**. ETA **Later** |
| 9 | **Recurring causes.** The same fingerprint fixed again and again on the host | Two occurrences within 14 days submit a Shikiru task that makes the setup reproducible (bootstrap script or a documented prerequisite), as a technical, decision-free task under class `prevention` (scenario 3) | **MVP**, in Phase C. In the test sample one environment cause came back five times |
| 10 | **Technical lead answers** (see the fourth triage route in §6) | Explicit route under class `decision.technical`, so design choices never reach the PO | **MVP** |
| 11 | **Product charter** | Written once in a 15-minute onboarding chat; answers questions before they are asked (§4) | **MVP** |
| 12 | **Goal queue and a limit on active goals.** Handing over several goals at once stretches lead time and exhausts quota | `max_active_goals` (default 2) and a priority order the PO can change (scenario 1). It queues goals, not tasks; Shikiru still schedules tasks | **MVP**: one number and one state |
| 13 | **Flow measurement.** "Faster" cannot be checked otherwise | PO wait time, lead time, autonomy share and rework rate (§13) | **MVP**: timestamps already exist |

### Risks found in the design itself

* **Untrusted text steering a repair.** Logs, web pages and package READMEs can carry hostile instructions. No Operator agent holds host rights, so the worst outcome is a bad plan. The step policy checks it (allow-listed executables and sources, no pipe-to-shell), a human sees it at the `ask` level, and a probe verifies the result. Research stays in a read-only profile that returns text only, plan execution carries no PO or Telegram credentials, and package-manager state is checked before and after.
* **Silent Operator failure.** The service restarts itself, but if it cannot start nobody is told. The external heartbeat from scenario 8 should be a documented recommended default, not an afterthought.
* **A misclassified decision.** If a product choice is labeled technical, the Operator decides something that was the PO's. Bounds: classes start low, `default` announces before applying, a veto demotes the class at once, and scope, cost, irreversible and access decisions can never leave `ask`. The reasoner's verdicts are logged for replay, and the Judge can be enabled if they prove unreliable.

## 15. Implementation effort

### Basis

Estimates are anchored on how Shikiru itself was built with AI agents (git and `tasks/plan.md`), not on guesses:

| Fact | Value |
|---|---|
| Shikiru core without UI (engine, domain, store, operator services, agents, plugins, CLI, MCP) | about 30k lines of source |
| Whole of Shikiru | 56k source and 67k test lines; 1,023 commits; 305 tasks in 36 days |
| Core delivered | about 95 tasks in about 14 days (17 Sep to 1 Oct) |
| Rework after "feature complete" | four remediation phases (7, 7b, 7c, 7d), about 40 tasks on top of about 55 feature tasks, so budget **0.5–0.7 tasks of rework per feature task** |
| Test code ratio | about 1.2 lines of test per line of source |

Caveat on the anchor: Shikiru's build record shows what agents did on Shikiru, not what Shikiru does on unfamiliar real work (see §16). Add a **20–30% contingency** for Shikiru defects found while using it for the first time on real work; the pessimistic case in the table below already assumes it.

Shikiri-yaku needs no TUI (the 16k-line dashboard and 3k-line UI layer were the largest and most review-heavy part of Shikiru) and delegates the entire development workflow. It does touch real external systems (Telegram, real agents, package managers, a host reboot), which can be faked less than Shikiru's own internals, so I expect **lower throughput per task**: about 4–6 tasks per day instead of Shikiru's 7.

### Scope: what the estimate covers

Included: scenarios 1–9 with Telegram only, the MVP items of §14 (including the charter, goal queue, autonomy dial and flow measures), macOS first. The Judge interface exists but is off.

**Cut under YAGNI, with the trigger that brings each back:**

| Cut | Trigger |
|---|---|
| Judge backends (Jev and Laya): only the interface is built | Replays show a frequent route that rules plus the reasoner handle badly, or a requirement for no data egress |
| Any channel besides Telegram. Scenario 9 is proven with two Telegram chats as separate destinations, because the logic is the same | A real second channel |
| Privileged `yaku-admin` helper. On macOS, Homebrew and user toolchains need no sudo. The helper is built when the first Linux or `sudo` host appears | First Linux host or a package that needs admin rights |
| Direct-API or local-model reasoner adapter. Shikiru's Claude and Codex plugins already serve both subscription and API login | A profile the plugins cannot serve |
| `yaku init` wizard. `yaku doctor` plus a documented `yaku.toml` is enough for one host | A second installation |
| Release command and probe, per-topic standing rules, bug subtype, ETA, hardening tasks (§14 *Later*) | Their listed triggers |
| E3 (idempotency key), E5 (project pause) in Shikiru | A duplicate task or a coarse-stop complaint in practice |

Net effect of the second review on the estimate: one instance per product (§3.1), the Judge cut, merging access needs into decisions and the plan-then-execute repair path remove more work than the dial, charter, goal queue, flow measures and minimal preview add. About two tasks fewer, which is within the noise of the estimate.

### Work breakdown

One *task* equals one Shikiru task: an Implementer and Leader loop ending in a reviewed, merged change. Line counts are for production code.

| Phase | Delivers | Feature tasks | Source lines | Usable result |
|---|---|---|---|---|
| **0 Prerequisites** | Shikiru E1 (held new tasks) and E2 (typed category), E4 only if a stable agent SDK is wanted now; test host, Telegram bot, `yaku.toml` skeleton | 4–6 (in Shikiru) | about 400 (in Shikiru) | Foundations |
| **A Relay** | Journal and migrations, config, service install, Watcher with MCP link, cursors and snapshot reconcile, engine liveness, Telegram in and out, the decision lifecycle with versions (one destination), Shikiru escalations turned into product-term decisions with a recommendation and buttons, `resolve_escalation`, milestone and digest templates, stall watchdog, nightly journal backup, optional heartbeat, flow timestamps, `yaku doctor` | 12 | 1.8k | **The PO answers Shikiru's escalations from Telegram and stops watching tmux.** Real escalation data starts to accumulate |
| **B Goals and autonomy** | Intent routing, the product charter and onboarding chat, Intake with the question gate and definition of ready, goal queue and limit, goal and item links, cross-component sequencing, multiple destinations and conflict handling, goal control, acceptance note with Accept/Change and minimal preview, the autonomy dial (policies, promotion, demotion, away mode), the technical-lead route, flow measures and `/flow` | 18 | 2.8k | Scenarios 1, 4, 6, 9. The PO can drive real work, and routine choices stop reaching the PO |
| **C Repair** | Complete rule triage, Reasoner with profiles, blocks and spend rules, case manager and attempt ladder, read-only `investigate` returning a plan and probe, step policy and plan execution in the Host module with the lock, fix decisions and graduation along the dial, health probes, access decisions and the technical contact, the prevention task | 12 | 2.4k | Scenarios 2, 3, 5, 7 |
| **D Hardening** | Ops reconcile for restart, orphan handling, engine auto-start rules, failure-injection drills (§13), security review of the plan and step-policy path, replay of old escalations through triage | 8, plus rework | 0.5k | Scenario 8 and the proof of §13 |
| **Total** | | **about 50 feature tasks (plus 4–6 in Shikiru)** | **about 7.5k** (tests about 9k) | |
| **Rework allowance** | Review and remediation rounds | **25–35 tasks** | | |

That is roughly a quarter of Shikiru's non-UI size, and about half of the 95 tasks Shikiru's core took (those 95 already include its remediation) before rework.

### Time

| Measure | Estimate | Notes |
|---|---|---|
| **MVP**: Phases 0, A, B and the retry, engine-recovery and health-probe slice of C | **2–3 calendar weeks** | About 35–40 tasks plus rework, usable for real goals with manual help on technical problems. The Phase A relay is usable after about a week (low confidence) |
| **v1**: all phases and the drills of §13 | **4–6 calendar weeks**, plan for 5 | About 85 tasks including rework at 4–6 a day is 14–21 working days, then a soak week on a real project |
| Optimistic / pessimistic | 3 weeks / 8 weeks | Pessimistic if real-agent or Telegram quirks need redesign, or quota windows throttle the build |
| **Human time** | About 1–2 hours a day, 40–60 hours in total | Reviewing the plan and escalations, answering decisions, running drills. About one day goes to setup: bot, test host, a sandbox project pair, a 15-minute charter chat |
| **Agent compute** | Not estimated | I have no usage figures to extrapolate from. Measure tokens and quota with Shikiru's usage view after Phase A and extrapolate. Expect it to be the limiting factor if the build shares a subscription with other work |

Confidence is medium for the task count and low for calendar time.

### What moves the estimate

* **Down:** reusing Shikiru's agent plugins as they are (without E4 the Operator writes its own small signal consumer, about two extra tasks); keeping the first channel and platform singular and the Judge off; building Phase B before C so the PO benefits early; running the later phases through the Operator itself once it can submit and report.
* **Up:** adding a Linux host (the helper, a second package manager, a second service manager); any scope creep past §14's MVP column; and tuning the dial's classification and promotion rules, which needs real decision history and cannot be sped up with more agents.
* **The biggest risk to the schedule is not code volume. It is the number of real-world integration failure modes (Telegram delivery, agent login expiry, package managers) that only drills reveal.** Budget Phase D and the soak week accordingly rather than trimming them.

### Order of work

Build in the order A, B, C, D so each phase ends with something the PO can use: first a relay that lets the PO answer Shikiru's escalations from Telegram in product terms (this alone ends tmux-watching and starts collecting the real escalation data the decision gate needs), then a goal-driven Operator with the charter and the dial, then a repairing one, then a trustworthy one. Do the Shikiru extensions E1 and E2 first, since Phases B and C depend on them, and keep each Operator task small enough to pass Shikiru's own review loop in one or two iterations.

## 16. Assessment: can Shikiru build it, and does it boost development?

**Key fact, confirmed by you:** Shikiru has so far only run on simple projects that verify its core functionality. It has no track record on real product work. The assessment below is built on that, and it lowers some of my earlier confidence.

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
| 2 | Keep live externals out of the agent loop. Telegram and package-manager tests are a human-launched smoke script, not part of `poe check` | The Codex `workspace-write` sandbox blocked a dev server and gave no browser in the test project. Network and host changes would fail the same way. Agents inherit your environment, so the bot token must not be in it |
| 3 | A disposable host (Docker image or VM) for the repair path | Testing "install a system package and recover" on your machine is unsafe; Shikiru's isolation is a convention, not a sandbox |
| 4 | Pin Shikiru while building with it. Develop E1 and E2 on a branch of the Shikiru repo and upgrade the build host deliberately | Entry points are written at install time; a moving engine under a running build is avoidable risk |
| 5 | Small tasks: about 300 lines of change each, with testable acceptance criteria | In the test project 21 of 42 done tasks needed two review rounds and one needed five |
| 6 | Pilot first and keep the manual fallback (above) | Shikiru is unproven on real work |

Phase 0 runs in two repositories (Shikiru: E1 and E2; Operator: scaffold copied from Shikiru's gate). Phases A to D follow in dependency order with `integration = "merge"` and `max_parallel = 2`. From Phase A on, the relay lets the PO answer the build's own Shikiru escalations from Telegram, and from Phase B the Operator can supervise the rest of its own build. That is a worthwhile test but must not be the only one.

### 16.2 Does it boost development?

**Honest answer: the mechanism is real and I can show it in the data, but the size of the boost on real product work is unknown, and it depends on how reliable Shikiru turns out to be.**

**What the data shows.** The only history on this machine is `test-project/.shikiru/state.db`, a small React/TypeScript app driven by Codex, 5 to 16 October 2026. I read a read-only copy. It is a *verification project*, so its numbers describe a testbed, not product work.

| Measure | Value |
|---|---|
| Tasks | 46 (42 done, 3 cancelled, 1 escalated). 15 submitted by you, 31 created by the Leader |
| Escalations raised | 28 (0.6 per task): 19 Leader `decision_required`, 7 integration conflicts or blocks, 2 cancelled dependencies |
| Time tasks waited for an answer | 631 task-hours in total. Half of the answered escalations waited over 4 hours; the longest 6.4 days; one is open after 155 hours |
| Your commands | 141 for 42 done tasks, inflated by testing of Shikiru itself (stops, take-overs) |

**What the escalations were.** About 15 to 17 of the 19 Leader decisions concerned the **environment**: no browser available for UI acceptance checks, a dev server that could not bind in the sandbox, missing `vite`/`react`. The same cause recurred (T-36 five times, T-41 four, T-35 twice). The agents wrote in the packets that they could not provision a browser themselves. One item was clearly product and access (E-27: ad provider, placement, consent). All seven integration blocks were cleared by a plain `retry`.

**What this proves, and what it does not.**
* It proves the **gap the Operator fills**: Shikiru's agents stop and wait for a person when the environment lacks something, and that wait dominated elapsed time. An Operator that installs the capability once, recognizes the repeat and applies routine retries would have removed most of those 28 interruptions in this sample.
* It does **not** prove the rate on real work. A test project that intentionally exercises a verification flow, with a UI acceptance step and a sandbox that has no browser, probably over-produces environment escalations. Real work will likely add more genuine product questions, scope disagreements and review rounds. I would not quote "90% fewer interruptions" as an expectation; it only describes this testbed.
* It says nothing about cost or code quality. The Operator removes waiting and interruptions. It does not make agents write better code, and mean time per done task here (309 minutes) is unchanged except for blocked time.
* The boost is **capped by Shikiru**. If Shikiru's own core loop proves unreliable on real work, the Operator only supervises that unreliability, more quietly.

**Costs the Operator adds:** reasoner usage for investigation, one more service to keep running, and new failure modes, a wrong repair plan and a misclassified decision. The attempt budgets, the pre-declared verification probe and the reporting of every action bound that.

### 16.3 Recommendation, adjusted for an unproven Shikiru

Do not commit to the whole Operator on the strength of this data. Stage it so evidence arrives before the expensive part is built, which is also what YAGNI asks.

1. **Stage 1, dogfooding (§16.4):** build the Operator's Phase 0 and A with Shikiru, and fix the Shikiru defects that block it with a second Shikiru instance in the Shikiru repo. Record escalations, waits and your commands. This is the first honest baseline.
2. **Stage 2, MVP Phases A and B:** the relay, goal intake, charter, decision flow and autonomy dial. These are useful whatever the escalation mix turns out to be, since they remove the need to watch tmux and decompose goals, and the dial removes the waiting that dominated the test sample.
3. **Stage 3, decision gate:** replay the real escalations from Stages 1 and 2 through the Phase A triage rules in shadow mode. **Build the repair path of Phase C beyond retries, engine recovery and health probes only if at least about half of real escalations are technical and routable to a rule or a repair.** If most turn out to be product decisions, the better investment is the decision flow, the dial and the acceptance note, and Phase C stays at routine retries and engine recovery.
4. **Stage 4:** hardening, drills and the soak week, as in §15.

Under this staging the Operator is built mostly with Shikiru, which doubles as its first real validation, and the largest and riskiest part (executing repair plans on the host) waits for evidence that it is needed.

### 16.4 Dogfooding protocol: build the Operator with Shikiru, fix Shikiru with Shikiru

**Assessment: worthwhile, and the strongest validation available.** It tests Shikiru on real work, produces the real escalation corpus that the §16.3 decision gate and the later calibration of the reasoner's classification (or an enabled Judge) need, and exercises the Operator's own multi-component case, since the two repositories are two Shikiru projects on one host. The cost is circularity: a Shikiru bug can disable the tool that would fix it. These rules contain that.

| # | Rule | Reason |
|---|---|---|
| 1 | **Two tracks.** A frozen *builder* Shikiru (a tagged, separately installed build, for example `uv tool install --from <tag>`) drives both projects. The Shikiru checkout under change is the *candidate* and is never installed editable on the builder host | An editable install of the checkout whose branches the engine merges would change the running engine under itself |
| 2 | **Promote deliberately.** A candidate becomes the builder only after `poe check` and the repository's release smoke script pass. Back up each project's `state.db` first, and keep the previous tag installed | Migrations only go forward, and the builder owns live state |
| 3 | **Order.** Do E1 and E2 first in the Shikiru repo, promote, then start the Operator's Phases A to D on the builder | The Operator's later phases depend on those changes |
| 4 | **Fix only what blocks or distorts the build.** Shikiru problems get a severity. Blockers and majors are fixed in the same week; the rest go to a list. Cap Shikiru-fix work at about 20% of tasks | Without a cap the project becomes Shikiru's backlog (305 tasks and counting) |
| 5 | **Human review on engine-critical paths** (recovery, command replay, integration, store migrations, escalation). Use different agents for leader and implementer than the ones that wrote the change where the ladder allows | Shikiru's own history needed four remediation phases after independent review. Self-review of the engine by the engine's own agents is the weakest point |
| 6 | **Manual fallback stays.** A task Shikiru cannot finish after one retry is run directly with the agent on the same branch | A broken builder must not block its own repair |
| 7 | **Coordinate quota and resources.** Run the two engines with `max_parallel` of 1 each (or 2 and 1), and watch for both hitting the same provider limit | Each engine keeps its own quota blocks, so two engines on one subscription can exhaust it together. Within one product the Operator at least sees both engines' quota blocks (scenario 7); note any collision as evidence |
| 8 | **Check test isolation in the pilot.** The first Shikiru-fix task must show that `poe check` (it spawns tmux and engines on private sockets) runs cleanly inside a task worktree beside a live engine | It is untested; discover it on a small task |
| 9 | **Log every Shikiru problem** in `docs/dogfooding-log.md`: symptom, evidence (escalation packet path), cause class, workaround, fix task. Keep the packets | This is the replay corpus for triage, the reasoner's classification (or an enabled Judge) and the decision gate |

Once the Operator runs, this same pair is its first product instance: one product, `shikiri-yaku`, with two components (`yaku` and `shikiru`). That also gives the multi-component path a real test.

Two things the pilot must establish before the plan is released: that a Shikiru-fix task can complete end to end under the builder (rule 8), and that a promotion with rollback works on a copy of a real state database (rule 2).

## 17. Earlier concepts (archive: initial, v2, v3)

The three archived concepts reached nearly the same architecture as this one: a standalone service, MCP-only access to Shikiru, a small journal, rules plus a decision model plus a reasoner, fingerprinted incidents with a shared budget, one decision identity across channels, restart reconciliation and a typed privileged helper. Differences worth recording:

**Taken over**

| Idea | From | Now in |
|---|---|---|
| Never use `accept` to clear an escalation; never lower acceptance criteria as "recovery"; repository and agent text is evidence, not instruction | Initial | §6 guardrails |
| A table of default responses per situation | Initial | §6 |
| A concrete decision message the PO would receive | Initial | Scenario 4 |
| No completion percentages from a changing task count | Initial | §4 |
| Curated agent environment, so the billing path never changes silently; no probing a blocked profile with inference | Initial | §10 |
| Telegram: save updates before advancing the offset; 24-hour retention means re-presenting open decisions | Initial | §11 |
| One-glance split of what the Operator handles and what needs a human | v2 | §4 autonomy contract |
| Narrower authority on a personal machine than on a dedicated one | v2 | §10 host mode |
| Confirm that work has really stopped before a disruptive change | v3 | Scenario 2 |
| Ignore results from an obsolete attempt; reconcile uncertain effects before a replacement starts | v3 | Scenario 3 fencing |
| Pin dependency versions and check adapter compatibility before unattended work | v3 | §11 `yaku doctor` |
| The reasoner proposes, the service validates and executes; application repairs stay Shikiru tasks | v3 | Scenario 2 and §10 (plan, step policy, Host execution) |

**Left out on purpose**

| Idea | From | Why |
|---|---|---|
| Every host repair as a typed verb | v2, v3 | Plans are shell steps checked by an allow-list step policy, with typed verbs only for privileged steps. Far less code and it copes with unfamiliar failures; the price is a policy that is an allow-list, not a proof (§12, item 4) |
| Required idempotency keys on `submit_task` as a Shikiru change | v3 | The embedded marker plus `list_tasks` covers a lost receipt. E3 stays optional |
| Several projects of several products in one service, with portfolio digests and cross-project incident links | v2, v3 | One instance per product (§3.1) |
| Decision model as a first-class part of every route from release one | v3 | E2 and the reasoner's classification cover triage. The Judge interface exists but is off until replays justify it |
| No decision model in the first release | Initial | Now the same position, for a different reason: E2 makes most of the model unnecessary. The interface is kept for the case in which the free-text remainder of `decision_required` proves too hard for the reasoner |
| MCP Tasks extension, ACP adapter | v2, v3 | Adopt only when they remove code |
