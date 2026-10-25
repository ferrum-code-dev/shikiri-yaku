# Shikiri-yaku: implementation plan and assessment

This file belongs to the [design](design.md). It holds the effort estimate and the assessment of whether Shikiru can build the Operator and whether the Operator speeds development. Section numbers continue from the design.

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

Included: scenarios [1](design.md#1-the-po-describes-a-goal)–9 with Telegram only, the MVP items of [§14](design.md#14-review-what-the-scenarios-missed) (including the charter, goal queue, autonomy dial and flow measures), macOS first. The Judge interface exists but is off.

**Cut under YAGNI, with the trigger that brings each back:**

| Cut | Trigger |
|---|---|
| Judge backends (Jev and Laya): only the interface is built | Replays show agents mislabeling `category` or `changes` often enough to matter, or a requirement for no data egress |
| Prevention tasks that change repositories to remove a recurring cause | Data showing that the same cause recurs often |
| Any channel besides Telegram. Scenario [9](design.md#9-several-channels) is proven with two Telegram chats as separate destinations, because the logic is the same | A real second channel |
| Privileged `yaku-admin` helper. On macOS, Homebrew and user toolchains need no sudo. The helper is built when the first Linux or `sudo` host appears | First Linux host or a package that needs admin rights |
| Any Operator-side model adapter. Model access is Shikiru's, and its Claude and Codex plugins already serve both subscription and API login | A rung the plugins cannot serve, which is then a Shikiru plugin task |
| `yaku init` wizard. `yaku doctor` plus a documented `yaku.toml` is enough for one host | A second installation |
| Release command and probe, per-topic standing rules, bug subtype, ETA, hardening tasks ([§14](design.md#14-review-what-the-scenarios-missed) *Later*) | Their listed triggers |

### Work breakdown

One *task* equals one Shikiru task: an Implementer and Leader loop ending in a reviewed, merged change. Line counts are for production code.

| Phase | Delivers | Feature tasks | Source lines | Usable result |
|---|---|---|---|---|
| **0 Shikiru requirements** | R1–R6 in the Shikiru repository ([§9](design.md#required-shikiru-changes)), in two steps: **0a** R1, R2, R5, R6 (before Phase A) and **0b** R3, R4 (before Phase B). Also the test host, Telegram bot and `yaku.toml` skeleton | 15 in Shikiru (about 9 in 0a, 6 in 0b) | about 1.8k in Shikiru | Shikiru offers the interface the Operator is designed against |
| **A Relay** | Journal and migrations, config, service install, Watcher with MCP link, cursors and snapshot reconcile, engine liveness, Telegram in and out, the decision lifecycle with versions (one destination), Shikiru escalations turned into product-term decisions with a recommendation and buttons, `resolve_escalation`, milestone and digest templates, nightly journal backup, optional heartbeat, flow timestamps, `yaku doctor` | 11 | 1.6k | **The PO answers Shikiru's escalations from Telegram and stops watching tmux.** Real escalation data starts to accumulate |
| **B Goals and autonomy** | Intent routing, the onboarding chat (charter and product model), Intake with the question gate and definition of ready, goal queue and limit, goal items and `ref`s, routing by the product model and per-component assessment, model refresh and routing-miss counting, held submission and release for cross-component order, multiple destinations and conflict handling, goal control, acceptance note with Accept/Change and minimal preview, the autonomy dial (four classes, promotion, demotion, away mode), the starred-option route for technical choices, flow measures and `/flow` | 16 | 2.4k | Scenarios [1](design.md#1-the-po-describes-a-goal), [4](design.md#4-a-real-product-decision), [6](design.md#6-the-po-wants-to-know-what-is-happening), [9](design.md#9-several-channels). The PO can drive real work, and routine choices stop reaching the PO |
| **C Repair** | Complete rule triage, case manager and attempt ladder, `inquire`-based investigation returning a plan and probe, step policy and plan execution in the Host module with the lock, fix decisions and graduation along the dial, health probes, access decisions and the technical contact | 8 | 1.7k | Scenarios [2](design.md#2-development-hits-a-technical-problem), [3](design.md#3-the-first-repair-does-not-work), [5](design.md#5-access-or-authority-is-missing), [7](design.md#7-an-agent-becomes-unavailable) |
| **D Hardening** | Ops reconcile for restart, orphan handling, engine auto-start rules, failure-injection drills ([§13](design.md#13-proving-it-works)), security review of the plan and step-policy path, replay of old escalations through triage | 7, plus rework | 0.4k | Scenario [8](design.md#8-the-computer-or-the-operator-restarts) and the proof of [§13](design.md#13-proving-it-works) |
| **Total** | | **about 57 feature tasks (42 in the Operator, 15 in Shikiru)** | **about 7.9k** (6.1k Operator, 1.8k Shikiru; tests about 9.5k) | |
| **Rework allowance** | Review and remediation rounds | **28–40 tasks** | | |

That is roughly a quarter of Shikiru's non-UI size, and about 60% of the 95 tasks Shikiru's core took (those 95 already include its remediation) before rework. About a quarter of the work is in Shikiru, which is where it reduces the Operator's size and risk.

### Time

| Measure | Estimate | Notes |
|---|---|---|
| **MVP**: Phases 0, A, B and the retry, engine-recovery and health-probe slice of C | **3–4 calendar weeks** | About 45 tasks plus rework (about 72 in all), usable for real goals with manual help on technical problems. The Phase A relay is usable after 0a and A, about 20 tasks, so after one and a half to two weeks (low confidence) |
| **v1**: all phases and the drills of [§13](design.md#13-proving-it-works) | **4–6 calendar weeks**, plan for 5 | About 85–97 tasks including rework at 4–6 a day is 14–24 working days, then a soak week on a real project. The Shikiru tasks touch engine-critical code and need human review, so expect the upper half |
| Optimistic / pessimistic | 4 weeks / 8 weeks | Pessimistic if real-agent or Telegram quirks need redesign, or quota windows throttle the build |
| **Human time** | About 1–2 hours a day, 45–65 hours in total | Reviewing the plan and escalations, answering decisions, running drills. About one day goes to setup: bot, test host, a sandbox project pair, a 15-minute onboarding chat |
| **Agent compute** | Not estimated | I have no usage figures to extrapolate from. Measure tokens and quota with Shikiru's usage view after Phase A and extrapolate. Expect it to be the limiting factor if the build shares a subscription with other work |

Confidence is medium for the task count and low for calendar time.

### What moves the estimate

* **Down:** reusing Shikiru's agent plugins, ladders and usage ledger through R4, so the Operator has no agent runtime to build; the product model and the four-class dial, which are smaller than the mechanisms they replace; keeping the first channel and platform singular and the Judge off; building Phase B before C so the PO benefits early; running the later phases through the Operator itself once it can submit and report.
* **Up:** R1 and R4 touch Shikiru's engine-critical paths (store migrations, command replay, agent turns), where human review is required ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru), rule 5) and where defects cost the most; adding a Linux host (the helper, a second package manager, a second service manager); any scope creep past [§14](design.md#14-review-what-the-scenarios-missed)'s MVP column; and tuning the dial's classification and promotion rules, which needs real decision history and cannot be sped up with more agents.
* **The biggest risk to the schedule is not code volume. It is the number of real-world integration failure modes (Telegram delivery, agent login expiry, package managers) that only drills reveal.** Budget Phase D and the soak week accordingly rather than trimming them.

### Order of work

Build in the order A, B, C, D so each phase ends with something the PO can use: first a relay that lets the PO answer Shikiru's escalations from Telegram in product terms (this alone ends tmux-watching and starts collecting the real escalation data the decision gate needs), then a goal-driven Operator with the charter and the dial, then a repairing one, then a trustworthy one. Do the Shikiru requirements first, in two steps: 0a (R1, R2, R5, R6) before Phase A, and 0b (R3, R4) before Phase B, since Phases B and C depend on them, and keep each Operator task small enough to pass Shikiru's own review loop in one or two iterations.

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
| 4 | Pin Shikiru while building with it. Develop R1–R6 on a branch of the Shikiru repo and upgrade the build host deliberately | Entry points are written at install time; a moving engine under a running build is avoidable risk |
| 5 | Small tasks: about 300 lines of change each, with testable acceptance criteria | Large or vague tasks cost review rounds; small, checkable ones keep Shikiru's review loop short (assumption, confirm in the pilot) |
| 6 | Pilot first and keep the manual fallback (above) | Shikiru is unproven on real work |

Phase 0 runs in two repositories (Shikiru: R1–R6; Operator: scaffold copied from Shikiru's gate). Phases A to D follow in dependency order with `integration = "merge"` and `max_parallel = 2`. From Phase A on, the relay lets the PO answer the build's own Shikiru escalations from Telegram, and from Phase B the Operator can supervise the rest of its own build. That is a worthwhile test but must not be the only one.

### 16.2 Does it boost development?

**Honest answer: the mechanism is clear, but nothing has been measured.** Shikiru has not been used on real product work, so the size of the boost is a set of assumptions, not a result. The first weeks of dogfooding ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)) are meant to confirm or refute them.

| # | Assumption | How it is checked |
|---|---|---|
| 1 | Elapsed time in Shikiru-driven development is dominated by waiting for a person (an escalation left unanswered for hours, overnight, or while the PO is busy), not by agent working time | PO wait time and the task-hours blocked meanwhile, against lead time ([§13](design.md#13-proving-it-works)) |
| 2 | A large share of escalations is routine: environment gaps (a missing tool, a dev server that cannot start in the sandbox, no browser for UI checks), integration blocks that a plain retry clears, and technical choices the brief and charter already answer. The rest are genuine product decisions | The escalation corpus from dogfooding, classified by cause ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru), rule 9) |
| 3 | The same environment cause tends to recur until the setup is made reproducible, so fixing it once removes a whole series of interruptions | Recurrence counts per fingerprint |
| 4 | Real work adds genuine product questions, scope disagreements and review rounds, so the avoidable share of interruptions is smaller than in a purely technical exercise. No target figure is promised | Autonomy share and the recommendation acceptance rate ([§13](design.md#13-proving-it-works)) |
| 5 | The Operator removes waiting. It does not make agents write better code or lower their cost | Rework rate and usage ([§13](design.md#13-proving-it-works)) |

**The boost is capped by Shikiru.** If Shikiru's own core loop proves unreliable on real work, the Operator only supervises that unreliability, more quietly.

**Costs the Operator adds:** inquiry usage for assessment and investigation, one more service to keep running, and new failure modes, a wrong repair plan and a misclassified decision. The attempt budgets, the pre-declared verification probe and the reporting of every action bound that.

### 16.3 Recommendation, adjusted for an unproven Shikiru

Do not commit to the whole Operator on the strength of assumptions. Stage it so evidence arrives before the expensive part is built, which is also what YAGNI asks.

1. **Stage 1, dogfooding ([§16.4](#164-dogfooding-protocol-build-the-operator-with-shikiru-fix-shikiru-with-shikiru)):** build the Operator's Phase 0 and A with Shikiru, and fix the Shikiru defects that block it with a second Shikiru instance in the Shikiru repo. Record escalations, waits and your commands. This is the first honest baseline.
2. **Stage 2, MVP Phases A and B:** the relay, goal intake, charter, decision flow and autonomy dial. These are useful whatever the escalation mix turns out to be, since they remove the need to watch tmux and decompose goals, and the dial removes waiting for the PO.
3. **Stage 3, decision gate:** replay the real escalations from Stages 1 and 2 through the Phase A triage rules in shadow mode. Stage 1 escalations predate R2 and have no typed fields, so label them by hand once. **Build the repair path of Phase C beyond retries, engine recovery and health probes only if at least about half of real escalations are technical and routable to a rule or a repair.** If most turn out to be product decisions, the better investment is the decision flow, the dial and the acceptance note, and Phase C stays at routine retries and engine recovery.
4. **Stage 4:** hardening, drills and the soak week, as in [§15](#15-implementation-effort).

Under this staging the Operator is built mostly with Shikiru, which doubles as its first real validation, and the largest and riskiest part (executing repair plans on the host) waits for evidence that it is needed.

### 16.4 Dogfooding protocol: build the Operator with Shikiru, fix Shikiru with Shikiru

**Assessment: worthwhile, and the strongest validation available.** It tests Shikiru on real work, produces the real escalation corpus that the [§16.3](#163-recommendation-adjusted-for-an-unproven-shikiru) decision gate and the later check of the agents' typed `category` and `changes` (or an enabled Judge) need, and exercises the Operator's own multi-component case, since the two repositories are two Shikiru projects on one host. The cost is circularity: a Shikiru bug can disable the tool that would fix it. These rules contain that.

| # | Rule | Reason |
|---|---|---|
| 1 | **Two tracks.** A frozen *builder* Shikiru (a tagged, separately installed build, for example `uv tool install --from <tag>`) drives both projects. The Shikiru checkout under change is the *candidate* and is never installed editable on the builder host | An editable install of the checkout whose branches the engine merges would change the running engine under itself |
| 2 | **Promote deliberately.** A candidate becomes the builder only after `poe check` and the repository's release smoke script pass. Back up each project's `state.db` first, and keep the previous tag installed | Migrations only go forward, and the builder owns live state |
| 3 | **Order.** Do R1, R2, R5 and R6 first in the Shikiru repo, promote, then start Phase A on the builder and run R3 and R4 in parallel. R1–R6 are planned changes, not fixes, so they do not count toward the cap of rule 4 | The Operator's phases depend on those changes |
| 4 | **Fix only what blocks or distorts the build.** Shikiru problems get a severity. Blockers and majors are fixed in the same week; the rest go to a list. Cap Shikiru-fix work at about 20% of tasks | Without a cap the project becomes Shikiru's backlog (305 tasks and counting) |
| 5 | **Human review on engine-critical paths** (recovery, command replay, integration, store migrations, escalation). Use different agents for leader and implementer than the ones that wrote the change where the ladder allows | Shikiru's own history needed four remediation phases after independent review. Self-review of the engine by the engine's own agents is the weakest point |
| 6 | **Manual fallback stays.** A task Shikiru cannot finish after one retry is run directly with the agent on the same branch | A broken builder must not block its own repair |
| 7 | **Coordinate quota and resources.** Run the two engines with `max_parallel` of 1 each (or 2 and 1), and watch for both hitting the same provider limit | Each engine keeps its own quota blocks, so two engines on one subscription can exhaust it together. Within one product the Operator at least sees both engines' quota blocks (scenario [7](design.md#7-an-agent-becomes-unavailable)); note any collision as evidence |
| 8 | **Check test isolation in the pilot.** The first Shikiru-fix task must show that `poe check` (it spawns tmux and engines on private sockets) runs cleanly inside a task worktree beside a live engine | It is untested; discover it on a small task |
| 9 | **Log every Shikiru problem** in `docs/dogfooding-log.md`: symptom, evidence (escalation packet path), cause class, workaround, fix task. Keep the packets | This is the replay corpus for triage, the agents' typed fields (or an enabled Judge) and the decision gate |

Once the Operator runs, this same pair is its first product instance: one product, `shikiri-yaku`, with two components (`yaku` and `shikiru`). That also gives the multi-component path a real test.

Two things the pilot must establish before the plan is released: that a Shikiru-fix task can complete end to end under the builder (rule 8), and that a promotion with rollback works on a copy of a real state database (rule 2).
