# Shikiri-yaku: implementation plan and assessment

This file belongs to the [design](design.md). It holds the effort estimate, the order of work, and the assessment of whether Shikiru can build the Operator and whether the Operator speeds development.

## 1. Effort

### Basis

Estimates are anchored on how Shikiru itself was built with AI agents (git and `tasks/plan.md`):

| Fact | Value |
|---|---|
| Shikiru core without UI (engine, domain, store, operator services, agents, plugins, CLI, MCP) | about 30k lines of source |
| Whole of Shikiru | 56k source and 67k test lines; 1,023 commits; 305 tasks in 36 days |
| Rework after "feature complete" | about 40 remediation tasks on top of about 55 feature tasks, so budget **0.5–0.7 tasks of rework per feature task** |
| Test code ratio | about 1.2 lines of test per line of source |

Shikiru's build record shows what agents did on Shikiru, not what Shikiru does on unfamiliar real work ([§2](#2-assessment)). The Operator touches real external systems (Telegram, agent CLIs, package managers, a host reboot), which can be faked less than Shikiru's internals, so expect **4–6 tasks a day** rather than Shikiru's 7, and add a **20–30% contingency** for Shikiru defects met on first real use.

### Scope

Included: the first release of the [design](design.md): the core, the product, technical and notice handlers, the install action, Telegram, macOS first. Everything in the design's [extensions](design.md#13-extensions) table is out of scope until its trigger appears.

### Work breakdown

One *task* is one Shikiru task: an Implementer and Leader loop ending in a reviewed, merged change.

| Phase | Delivers | Feature tasks | Source lines | Usable result |
|---|---|---|---|---|
| **0 Setup** | [S1](design.md#11-shikiru-what-is-used-what-is-needed) (structured escalation view) in the Shikiru repository; Operator scaffold with Shikiru's quality gate, the `AGENTS.md` from [§12](design.md#12-engineering-rules), a test bot and the `config.toml` skeleton | 1 in Shikiru, 1 in the Operator | about 0.2k | Shikiru offers what the Operator needs; the rules are enforced from the first commit |
| **A Relay** | Config and journal; ports, composition root, `run`, `doctor` and service install (with the login-shell PATH); the Shikiru MCP adapter with links and reconnect; the event source with cursors and first sync; the liveness source; signals and the classifier rules; Router, loop guard and scheduler; the product handler with templates and answers (needs S1); command confirmation and "resolved elsewhere"; Telegram send, edit and size limits; Telegram inbound, dedupe, offsets and allow-list; `/status`, `/open`, `/help`; `/goal` with its restart rule; wait-time measures | 14 | 1.3k | **The PO answers every Shikiru escalation from the phone and stops watching tmux.** Real escalation data starts to accumulate |
| **B Technical basics** | Retry; quota wait with `WaitUntil`; engine restart (including `engine_error`); the notice handler and the technical handler's messages | 4 | 0.3k | Routine technical escalations stop reaching the PO |
| **C Install** | Reasoner spike (tools-off and schema flags on Claude and Codex); Reasoner interface, adapter and the `assess_blocker` job with host facts; name validation and recipes; the approval flow on `ops`; the Executor (login shell, closed stdin, timeout, process group, host lock); restart handling of interrupted installs | 7 | 0.6k | Missing tools are installed with one tap |
| **D Hardening** | The drills of [§15](design.md#15-proving-it-works); the contract-test script against a real Shikiru with its fake agent; crash-point tests; security review of the install path | 4 | 0.1k | Scenario 8 and the proof |
| **Total** | | **about 31 feature tasks (30 in the Operator, 1 in Shikiru)** | **about 2.5k** (tests about 3k) | |
| **Rework allowance** | Review and remediation rounds | **15–21 tasks** | | |

### Time

| Measure | Estimate | Notes |
|---|---|---|
| **Relay** (Phases 0 and A) | **about 1 week** | About 16 tasks plus rework. Usable from the day it runs |
| **First release** (all phases) | **2–3 weeks**, plan for 3 | About 46–53 tasks including rework at 4–6 a day is 8–13 working days, then a soak week on a real project |
| Optimistic / pessimistic | 2 weeks / 5 weeks | Pessimistic if Telegram or agent CLI quirks need redesign, or quota windows throttle the build |
| **Human time** | About 1 hour a day | Reviewing the plan and escalations, approving, running drills. Half a day of setup: bot, test host, a sandbox project pair |
| **Agent compute** | Not estimated | No usage figures exist to extrapolate from. Measure with Shikiru's usage view after Phase A |

Confidence is medium for the task count and low for calendar time. **The largest schedule risk is not code volume but real-world failure modes** (Telegram delivery, agent login expiry, package managers) that only drills reveal. Keep Phase D and the soak week.

### Order of work

0, A, B, C, D. Each phase ends with something the PO uses. A is the core and stays fixed afterwards; B and C are the first extensions and prove that adding one does not change the core. S1 gates only the product handler task of A; the Watcher, journal and Telegram tasks do not wait for it. Keep each task small (about 300 lines of change) with testable acceptance criteria.

## 2. Assessment

**Key fact:** Shikiru has no track record on real product work yet. Every figure about benefit below is an assumption to be tested.

### 2.1 Can it be built with Shikiru?

**Feasible, and a sensible first real trial, with a fallback.** This host has what Shikiru needs (Shikiru, Python, tmux, git, uv, the `claude` and `codex` CLIs, `gh`). The Operator is small and non-visual, specification-driven, and can reuse Shikiru's quality gate, `SubprocessRunner`, `Clock` and SQLite patterns. A real Shikiru engine running its **fake agent** gives a deterministic Shikiru to watch and escalate, at no model cost.

Building the Operator would be Shikiru's first real project, so expect Shikiru defects during the build. That is useful data and the main schedule risk.

| # | Condition | Why |
|---|---|---|
| 1 | Prepare the repo first: the plan and `AGENTS.md` in one commit, then `shikiru init` with `project.trackers` set to `tasks/plan.md` and `tasks/todo.md` | Shikiru needs a git root with a commit; the first plan is human-reviewed |
| 2 | Keep live externals out of the agent loop. Telegram and package-manager tests are a human-launched smoke script, not part of `poe check` | Agents run with your privileges; Codex's sandbox may block network; the bot token must not be in their environment |
| 3 | A disposable host (Docker image or VM) for the install drills | Installing packages on your own machine as a test is unsafe |
| 4 | Pin Shikiru while building with it | A moving engine under a running build is avoidable risk |
| 5 | Pilot the first two or three tasks, reviewed line by line, and keep a manual fallback: a task Shikiru cannot finish after one retry is run directly with the agent on the same branch | Shikiru is unproven on real work; a Shikiru defect must not block the Operator |

### 2.2 Does it boost development?

**The mechanism is clear; nothing is measured yet.**

| # | Assumption | How it is checked |
|---|---|---|
| 1 | Elapsed time in Shikiru-driven development is dominated by waiting for a person, not by agent working time | PO wait time against task lead time ([§15](design.md#15-proving-it-works)) |
| 2 | A good share of escalations is routine: short-lived agent errors, integration blocks a retry clears, quota waits, missing tools | The escalation mix by reason and label |
| 3 | Answering from the phone is much faster than noticing an escalation in tmux | Wait time before and after the relay |
| 4 | The Operator removes waiting; it does not make agents write better code or lower their cost | Rework and usage |

**The boost is capped by Shikiru.** If Shikiru's core loop proves unreliable on real work, the Operator only reports that unreliability faster.

**Costs the Operator adds:** one more service, a few cheap model calls, and two new failure modes: a misrouted escalation (bounded by "unsure means product" and by measuring misroutes) and a wrong host command (bounded by commands that code builds from validated names, the PO's approval and the check).

### 2.3 Staging

1. **Relay first** (Phases 0 and A), built with Shikiru as in [§2.4](#24-dogfooding). It is useful whatever the escalation mix turns out to be, and it starts recording that mix.
2. **Technical basics** (Phase B) next. Cheap, and retries and quota waits are expected to be common.
3. **Gate before Phase C.** Look at the recorded escalations. Build the install path if missing tools appear among them; otherwise move C after whichever extension the measures point to.
4. **Extensions** in the order the measures suggest: the most frequent kind of signal that still reaches the PO, and the most frequent kind of approval the PO gives unchanged.

### 2.4 Dogfooding

**Build the Operator with Shikiru, and fix Shikiru with Shikiru.** It tests Shikiru on real work, produces the escalation data the gate needs, and exercises the multi-component case, since the two repositories are two Shikiru projects on one host. The risk is circularity: a Shikiru bug can disable the tool that would fix it. These rules contain it. Normally one pinned Shikiru drives both projects; rules 1 to 3 apply when a Shikiru change is built.

| # | Rule | Reason |
|---|---|---|
| 1 | **Two tracks when Shikiru itself changes.** A pinned *builder* Shikiru (a tagged, separately installed build) drives both projects. The Shikiru checkout under change is the *candidate* and is never installed editable on the builder host | An editable install would change the running engine under itself |
| 2 | **Promote deliberately.** A candidate becomes the builder only after `poe check` and the release smoke script pass. Back up each project's `state.db` first and keep the previous tag installed | Migrations only go forward, and the builder owns live state |
| 3 | **S1 is the only planned Shikiru change.** It is small and read-only, so it is promoted before the product handler task of Phase A, and the rest of Phase A does not wait for it | Keeps the Operator's start independent of Shikiru work |
| 4 | **Fix only what blocks or distorts the build.** Blockers and majors in the same week, the rest to a list. Cap Shikiru-fix work at about 20% of tasks | Otherwise the project becomes Shikiru's backlog |
| 5 | **Human review on Shikiru's engine-critical paths** (recovery, command replay, integration, store migrations, escalation) | Self-review of the engine by its own agents is the weakest point |
| 6 | **Coordinate quota.** Run the two engines with `max_parallel` of 1 each, and watch for both hitting the same provider limit | Each engine keeps its own quota blocks |
| 7 | **Log every Shikiru problem** in `docs/dogfooding-log.md`: symptom, escalation packet path, cause, workaround, fix task | The replay corpus for the classifier and the gate |

Once the relay runs, this pair becomes its first product instance: one product with two components, `shikiri-yaku` and `shikiru`. From then on the Operator relays the escalations of its own build.
