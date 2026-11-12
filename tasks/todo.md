# Todo

## Current work: release documentation cleanup

R1 to R9 replace the T-81 parent request. Details and shared constraints are in tasks/plan.md.

### R1

- [x] R1. Document installation and instance configuration

## Acceptance criteria

- README.md gives an installation and first-run sequence for the existing Python 3.14 package, Shikiru prerequisite, instance directory, protected token file, doctor and foreground run, without claiming an unverified package release exists.
- The documented TOML parses with load_settings using a temporary fake token and local paths; required keys, optional settings/defaults, local-only components, owner IDs, forum prerequisites and Reasoner syntax match the implementation.
- Only README.md and this task's checklist entry change; no host setup, live service, network probe or real secret is used for verification.
- uv run poe check passes; unrelated failures are reported with evidence.

### R2

- [x] R2. Document daily operation and recovery

## Acceptance criteria

- README.md covers current global option placement, CLI verbs and JSON output, /status, /open, /help and /goal in private chats and topics, confirmation/Details/Stop reminders behavior and exact-command install approval.
- Service instructions distinguish writing a unit from activating it and cover macOS launchd and Linux user systemd; topic adoption/forgetting, duplicate-topic risk, doctor, logs, configuration changes and uncertain restart recovery match current code.
- README.md states the current safety and operational limits, including data sent to Telegram, authorized owners, optional-model behavior, supported install recipes, uncertain goal submissions and clean-stop timing, without speculative roadmap promises.
- uv run poe check passes; unrelated failures are reported with evidence.

### R3

- [x] R3. Clean core contract and journal references

## Acceptance criteria

- Domain, ports and journal documentation contains no obsolete design section numbers or historical task citations and still explains relevant safety/recovery invariants.
- Python syntax trees excluding docstrings are unchanged in edited files; SQL, migration data, stored identities, public interfaces and other runtime literals are unchanged.
- The comment rule remains satisfied without removing a useful why-comment merely to satisfy a search.
- uv run poe check passes; unrelated failures are reported with evidence.

### R4

- [x] R4. Clean application and adapter references

## Acceptance criteria

- The scoped source files contain no obsolete design section or historical-task citations; retained comments explain the actual behavior without assuming removed prose.
- Python syntax trees excluding docstrings are unchanged, including prompts, command recipes, error text, settings, composition, imports and public interfaces.
- Any behavior mismatch discovered while editing is reported rather than fixed in production code.
- uv run poe check passes; unrelated failures are reported with evidence.

### R5

- [x] R5. Clean test script and gate references

## Acceptance criteria

- Tests, scripts and gate descriptions no longer cite obsolete design section numbers or historical build phases; behavior explanations, fixture provenance and human-run script instructions remain useful.
- Test assertions, fixture payloads, executable statements, test collection and script arguments are unchanged; any test-name adjustment updates retained drill mappings in the same change.
- pyproject.toml changes only comments and human-readable gate/contract descriptions; dependencies, banned APIs, ignores, contract boundaries and gate commands retain the same enforcement.
- uv run poe check passes; unrelated failures are reported with evidence.

### R6

- [x] R6. Remove migrated operational detail from the design

## Acceptance criteria

- The old configuration example, user message/button walkthroughs, recipe/tutorial details and long signal/rule/handler operational tables are removed from docs/design.md, with user-facing guidance available in README.md.
- The intermediate design still explains the signal-to-handler/channel flow, fallback, typed outcomes and approval boundary, and has no broken internal links caused by this pruning.
- Only documentation and this task's checklist entry change; missing or contradictory old-design details are resolved from implementation or explicitly reported.
- uv run poe check passes; unrelated failures are reported with evidence.

### R7

- [x] R7. Finish the concise purpose and architecture document

## Acceptance criteria

- docs/design.md is a concise description of the implemented purpose and architecture, including source/classifier/router/scheduler/handler/reply/channel roles, SQLite recovery and registration in bootstrap.
- The final design contains no speculative roadmap, implementation staging, no-code claim, duplicated user setup guide or stale numbered cross-reference; retained internal links resolve.
- Architecture descriptions match the current ports, outcomes and composition, and preserve the approval and stored-argument guarantees without introducing new requirements or behavior.
- uv run poe check passes; unrelated failures are reported with evidence.

### R8

- [x] R8. Retire obsolete planning artifacts and contributor citations

## Acceptance criteria

- docs/plan.md and tasks/reasoner-spike.md are removed; useful current Reasoner constraints, contributor validation instructions and fixture provenance are retained in appropriate existing documentation.
- AGENTS.md accurately points to the concise architecture, README and active trackers without weakening gate, dependency, safety or task-size instructions.
- The drill map and fixture READMEs have no broken references to deleted files or removed design sections; human-only checks remain explicitly outside the offline gate.
- uv run poe check passes; unrelated failures are reported with evidence.

### R9

- [x] R9. Clean historical tracker references and verify documentation

## Acceptance criteria

- tasks/plan.md and tasks/todo.md retain all active release entries and historical completion states, clearly separate current work from historical implementation records, and no longer depend on deleted artifacts or obsolete design section numbers.
- A repository-wide audit finds no broken retained local Markdown links/anchors or obsolete numbered-design citations in maintained docs, source, tests, scripts or gate descriptions; intentional runtime tracker text and historical records are distinguished from documentation dependencies.
- Final diff review confirms production behavior, prompts, public interfaces, SQL/migrations, dependencies, assertions and gate enforcement are unchanged, and reports any unresolved mismatch or unavailable live verification.
- uv run poe check passes; unrelated failures are reported with evidence.


Details and acceptance criteria are in `tasks/plan.md`. Mark a task `[x]` only when its change is
merged and `uv run poe check` passes.

## Planned work: README readability

R10 and R11 replace T-126. Details and shared constraints are in `tasks/plan.md`.

### R10

- [ ] R10. Condense setup and reference guidance

## Acceptance criteria

- README.md presents the product purpose, prerequisites, installation, instance creation, configuration, command line and chat commands in concise, scannable sections without omitting supported instructions.
- All documented TOML keys, defaults, command syntax, Telegram requirements and Reasoner ladder syntax remain consistent with the implementation.
- The change is limited to README.md, docs/reference.md (where T-129 moved the full reference detail) and this task's checklist entry; production code, tests, package metadata and service state are unchanged.
- uv run poe check passes; unrelated failures are reported with evidence.

### R11

- [ ] R11. Condense operations, recovery and safety guidance

## Acceptance criteria

- README.md presents approvals, automatic actions, service operation, recovery, local state, limits and contributor validation in concise, scannable sections without omitting essential instructions.
- Documented safety and recovery behavior remains accurate, including owner authorization, exact-command install approval, topic repair, uncertain goal handling and clean-stop behavior.
- The complete README has valid retained local Markdown links and headings, and uses the Shikiru README only as a readability reference.
- The change is limited to README.md, docs/reference.md (where T-129 moved the full reference detail) and this task's checklist entry; production code, tests, package metadata and service state are unchanged.
- uv run poe check passes; unrelated failures are reported with evidence.

## Planned work: service command group

S1 to S5 move the service commands into a `service` command group. Details and shared constraints
are in tasks/plan.md.

### S1

- [x] S1. Group the service commands under `service`

### S2

- [x] S2. Add `service start` and `service stop`

### S3

- [x] S3. `service install` starts the service

### S4

- [x] S4. Add `service status`

### S5

- [x] S5. Add `service uninstall`

## Planned work: the Expert

X1 to X7 add the Expert, a read-only agent the PO talks to in a forum topic. Details and shared
constraints are in tasks/plan.md.

### X1

- [x] X1. Describe the Expert in the design

## Acceptance criteria

- docs/design.md describes the Expert completely, and nothing in it contradicts the new text (the Reasoner's rule on file reading stays, as a rule of the Reasoner).
- The proposed Expert design file is deleted and no tracked file refers to it, except tasks/plan.md.
- Only documentation changes. `git diff --check` is clean.

### X2

- [x] X2. Expert agents and ladder

## Acceptance criteria

- A table test fixes the argument list of each adapter for a first turn and a resumed turn, including every flag that removes the agent's own web tools and write access.
- The ladder is tested with fake rungs: it stays on the last rung that answered, resumes within a provider, hands off across providers with findings and the last `handoff_turns` turns, skips a cooled-down rung, and reports when no rung answers.
- An answer outside the schema or the scope is never returned to the caller.
- Without `[expert]` nothing new runs and `doctor` is unchanged.
- uv run poe check passes; unrelated failures are reported with evidence.

### X3

- [x] X3. `/ask` opens a conversation and answers

## Acceptance criteria

- `/ask` in a component topic scopes the conversation to that component, in General to all, tested with fakes for the Channel, the topics and the Expert.
- A duplicate delivery of the same update opens one conversation and one topic.
- Each refusal and the shared-group rule are tested as a table.
- Crash points: after the journal row, after the topic, during the turn. None opens a second topic or runs a turn twice.
- `/goal`, `/status`, `/open` and `/help` outside Expert topics behave as before.
- uv run poe check passes; unrelated failures are reported with evidence.

### X3a

- [x] X3a. Reply to each `/ask` with its topic button

## Acceptance criteria

- With concurrent `/ask` commands, each button message names its own command as the message it replies to, and its button opens the topic of its own conversation.
- The reply target survives a failed send. The Telegram payload carries it with `allow_sending_without_reply`, so a command the PO deleted does not lose the button.
- uv run poe check passes; unrelated failures are reported with evidence.

### X4

- [x] X4. Conversation turns and topic commands

## Acceptance criteria

- Two messages sent during a running turn are answered in order, each once.
- A second turn resumes the session of the first; the effort set by `/effort` reaches the agent's argument list.
- A message in an Expert topic from someone who is not an owner starts nothing.
- Commands of other topics are not available in an Expert topic, and Expert commands are not available elsewhere.
- uv run poe check passes; unrelated failures are reported with evidence.

### X5

- [x] X5. End of a conversation

## Acceptance criteria

- Each of the three endings, and the failed send, leaves no row, no directory and no topic of that conversation, and touches no other conversation or component topic.
- Expiry counts from the last message of the conversation, by the Clock.
- A deletion Telegram refuses is retried later and reported once; the rows stay until it succeeds or the topic is found gone.
- Without the right, start-up fails with a message that names it; without `[expert]` the right is not required.
- uv run poe check passes; unrelated failures are reported with evidence.

### X6

- [x] X6. Task proposals with Submit all and Reject all

## Acceptance criteria

- Submit all issues exactly one `submit_task` per task, to the component the task names, tested with a fake ShikiruClient.
- Two taps, or a tap after a restart, submit nothing twice.
- A crash after some tasks were submitted leaves the rest unsubmitted or unknown, never repeated, and the PO is told which.
- A tap on a voided or rejected proposal submits nothing and says so.
- uv run poe check passes; unrelated failures are reported with evidence.

### X7

- [x] X7. Web access through grants

## Acceptance criteria

- A host without a grant is never contacted: tested with a fake transport, including a redirect from a granted host to another one and a URL that is not `https`.
- One request per host and turn is shown once; two taps store one grant and start one turn.
- A grant given in one conversation serves the next conversation without a question.
- The argument lists of X2 still remove every other web tool, and the web tool is the only MCP server the agents get.
- uv run poe check passes; unrelated failures are reported with evidence.

## Planned work: the Muse Code agent

M1 to M5 add Muse Code as a third agent of the Reasoner and the Expert. Execute each entry as a
separate task in order; M2 must establish the component write boundary before M5 starts. Details and
shared constraints are in tasks/plan.md.

### M1

- [x] M1. Move the CLI adapters into one module per agent

## Acceptance criteria
- The existing Reasoner and Expert tests pass with only their imports and construction calls changed; no assertion changes.
- No agent module imports another, and `common.py` and `registry.py` contain no agent name; a test enforces it.
- `bootstrap.py` and `settings.py` contain no agent name. An unknown adapter is rejected with the same message as before, from `doctor` and from `run`.
- A test registers a fake agent through the registry and runs a rung built for it, without editing `common.py`.
- import-linter passes and the contracts are not weakened.
- uv run poe check passes; unrelated failures are reported with evidence.

### M2

- [x] M2. Probe Muse for the Expert and capture fixtures

## Acceptance criteria
- `edit_file` and `write_file` are tried under the Expert's flags in the scratch directory, in a temporary directory outside it and in a directory that stands for a component, by absolute path, by a symlink and by a `../` path; a subagent, `monitor` and `workflow` are tried too. The report states where each succeeds. If a component can be written by any route, M5 is not started.
- The results match the hand probe recorded in tasks/plan.md, or the report says how they differ. Another installed `muse` version is run over the same probes, or the report says that only 1.4.15 was checked.
- The fixtures include the refusals `absolute path is outside the workspace`, `path escapes workspace` and `tool policy denied shell execution`.
- Where `add_memory` writes is confirmed, and the report says whether the conversation's directory keeps it to one memory directory per conversation.
- The real `_expert-web` server is loaded through `-c mcpServers.expert_web=...`, its tool is called, and an ungranted host is refused as in X7.
- Three turns with one `--session-id` and a schema resume correctly; the session id in the stream equals the one passed.
- A new UUID, a used UUID and a SIGTERM then resume are recorded with exit code and last record.
- Redacted fixtures for success with usage, each seen failure text, an interrupted run and a policy-denied tool call are committed under `tests/fixtures/muse/` with a README.
- Nothing outside temporary directories and `tests/fixtures/muse/` is written; probe directories are removed. `git diff --check` is clean.

### M3

- [x] M3. Muse Reasoner rung

## Acceptance criteria
- A table test fixes the argument list: required flags present, `--disable-write`, `--trust-workspace`, `--yolo`, `--disable-sandbox` and `--disable-approval` absent, the prompt last, the effort passed only when named.
- Fixture cases: valid answer, failed terminal, no terminal, schema mismatch, non-zero exit, timeout, non-JSON output; each is a `ReasonerError` naming the rung.
- The call record has summed token counts and `None` for duration and cost, and is `None` without `run.usage`.
- `muse:<model>[:<effort>]` is accepted in a ladder and shown by `doctor` and `/status`.
- README.md and docs/reference.md list `muse` and its limits.
- uv run poe check passes; unrelated failures are reported with evidence.

### M4

- [x] M4. Doctor and the owner's check of the Muse rung

## Acceptance criteria
- `doctor` names a missing `muse` binary and a failing `muse --version` as a failed probe, in the form used for Claude and Codex.
- The report lists the owner's checks that tests do not run: `doctor` under the service environment, a real Reasoner call, and `muse login` as the service's user.
- README.md says how to give the service a `PATH` that finds `muse`, if the probe fails only because of it.
- uv run poe check passes; unrelated failures are reported with evidence.

### M5

- [ ] M5. Muse Expert rung

## Acceptance criteria
- Started only if M2 shows that a component directory cannot be written under the Expert's flags by any route; otherwise stop and report.
- A scope with a component under the system temporary directory is refused before Muse runs; components elsewhere are accepted.
- Every turn runs with `request.directory` as its working directory; the schema and prompt files are in a temporary directory inside it, removed after the turn.
- README.md says that Muse's per-workspace memory for a conversation outlives it and where it is.
- A table test fixes the argument lists for a first and a resumed turn: required flags present, `--disable-write`, `--yolo`, `--disable-sandbox` and `--disable-approval` absent, the prompt file inside the scratch directory, and the web tool the only MCP server.
- A first turn passes a fresh UUID and returns it; a resumed turn returns the stored id; a stream with a different or missing session id is a failure.
- Schema, scope, failed-run, no-terminal, non-zero-exit and timeout failures are never returned; the ladder resumes and hands off with Muse as one provider.
- The web-grant tests of X7 pass with a Muse rung in the ladder.
- README.md and docs/reference.md state the rung's limits; the report lists the docs/design.md places that must change, without editing it.
- uv run poe check passes; unrelated failures are reported with evidence.

## Historical implementation records

The entries below from Phase 0 (T0) to Phase E are historical records. They are not current guidance,
and their § numbers refer to the design before the R6 and R7 rewrites.

## Phase 0: setup

- [x] T0. Scaffold with the quality gate

## Phase A: relay

- [x] A1. Settings and instance directory
- [x] A2. Journal
- [x] A3. Signals, classification and extensions
- [x] A4. Ports, composition root and CLI (ports, Clock, bootstrap entry point, run and doctor in; lint-imports green)
- [x] A4b. Install service (`service install` writes the launchd plist or systemd unit; doctor shows a held lock as running)
- [x] A5a. Shikiru MCP link and read verbs
- [x] A5b. Shikiru commands, `start` and the contract check
- [x] A6. Event source
- [x] A6b. First sync opens pull requests that are waiting
- [x] A7. Liveness and health sources (runtime synchronizes lifecycle events before down decisions)
- [x] A8a. Loop guard and journal queries
- [x] A8b. Router
- [x] A8c. Scheduler (notice reminder eligibility registered in bootstrap)
- [x] A8d. Source cleanup and failing sources
- [x] A8e. Dispatcher and start-up recovery
- [x] A9. Product handler
- [x] A10a. Recoverable outbox
- [x] A10b. Telegram send and edit (adapter, size limits, terminal and secret refusal with notices, journaled deletion; fixtures documentation-based, see tests/fixtures/telegram/README.md)
- [x] A10c. Composition and the source-to-delivery test
- [x] A10d. Bot API transport
- [x] A11a. Inbound transport and parsing
- [x] A11a2. Durable inbound ingestion
- [x] A11b. Replies
- [ ] A11c. Polling, answers and composition
  - [x] A11c1. Polling and delivery job, unreadable-batch health signal, offline composition test
  - [x] A11c2. Delete secret inbound messages in the chat (journal the attempt first)
  - [x] A11c3. "Already resolved: … at …" from the journal (resolve op's updated_at and option label)
  - [x] A11c4. Journaled edits that remove the buttons from every copy after a later answer
- [x] A12a. Rejected, queued and command-id confirmation
- [x] A12b. Start-up reconcile and lost commands
- [x] A12c. Resolved elsewhere (after A11c4)
- [x] A13. Status, open, help and measures
- [x] A14. `/goal`

## Later

- [x] B1. Destinations with topics
- [x] B2. Topic reconcile at start-up
- [x] B3. Inbound in topics
- [x] B4. Telegram topics belong to the adapter
- [x] C1. Infrastructure handler and Retry
- [x] C2. Quota wait
- [x] C3. Engine restart
- [x] C4. Review handler
- [x] C5. Review closing
- [x] C6. Notice handler
- [x] C7. Phase C paths through the composed app
- [x] C8. Fixes from the first live run
- [x] C9. Readable messages
- [x] C10. An engine that is up again closes its signal
- [x] C11. A message in a topic is not a reply to the topic
- [x] C12. Commands answer when they have nothing to show
- [x] Phase D: Install
  - [x] D1. Reasoner spike (note removed; its constraints are in `src/shikiri_yaku/adapters/reasoner_cli.py`)
  - [x] D2. Reasoner adapters and `assess_blocker`
  - [x] D3. Ladder and the safe tool list
  - [x] D3b. Wiring, host facts, health and doctor
  - [x] D4. Names, recipes and the existence check
  - [x] D5. Install approval
    - [x] D5b. Proposal persistence and host managers
    - [x] D5c. Install action and approval message
      - [x] D5c1. Install op payload, journal port and callbacks
      - [x] D5c2. Install preparation
      - [x] D5c3. Persisted approval and outbox delivery
    - [x] D5d. Install and Decline replies
      - [x] D5d1. Conditional install decisions
      - [x] D5d2. Callback routing and origin binding
      - [x] D5d3. Install decision effects
    - [x] D5e. Composition, `/open` and end-to-end approval
      - [x] D5e1. Journaled install approval replay in `/open`
      - [x] D5e2. Production composition and end-to-end approval
  - [x] D6. Executor and resolution
  - [x] D7. Interrupted installs and measures (component removal, lost-ack, durable rejection retry)
- [x] Phase E: Hardening
  - [x] E1. Recovery of an uncertain topic creation
  - [x] E2. Drills as tests
  - [x] E3. Recording real Telegram replies
  - [x] E4. Install path hardening from the security review
  - [x] E5. Command line in Shikiru's style
  - [x] E6. A cancelled task closes its open signals
  - [x] E7. A replaced message disappears
  - [x] E8. Reconcile an answer by Shikiru's real resolution text
  - [x] E9. Every signal of a component goes to its topic
  - [x] E10. The Acknowledge button says what it does
  - [x] E11. A group may have other members
  - [x] E12. Adopt a topic that was made by hand
    - [x] E5a. Typer app, global options, product resolution, flat verbs, telegram group
    - [x] E5b. Help palette and help layout
    - [x] E5c. Doctor lines and summary, error lines, colour on a terminal only
    - [x] E5d. Stats as titled rich tables
    - [x] E5e. --json for doctor and stats
  - [x] E13. Findings from the topics documentation
  - [x] E14. No channel names in the CLI checks and the journal
  - [x] E15. `/status` and `/open` in a component topic show only that component
  - [x] E16. Several instances in one group (`shared_group`)
  - [x] E17. `service restart` command and `poe deploy`
  - [x] E18. Deploy a tagged release, roll back on failure
  - [x] E19. `doctor` checks the Shikiru version
  - [x] E20. Open-source files
  - [x] E21. CI
  - [x] E22. GitHub Release on a version tag
  - [x] E23. First-run messages and install from a release
  - [x] E24. Deploy verification waits for the service
  - [x] E25. The help test does not depend on the CI environment
  - [x] E26. The release waits for a green CI on the tagged commit
  - [x] E27. Release notes from commits
  - [x] E28. One-command release
  - [x] E29. `update --wheel` replaces `poe deploy`
  - [x] E30. `update` downloads the newest release
  - [x] E31. The version comes from the git tag
  - [x] E32. Remove `update`, the release scripts and the pins
  - [x] E33. The release workflow: check CI once, publish to PyPI
  - [x] E34. Back up the journal before a migration
  - [x] E35. A failed journal backup leaves no file that looks like a backup
  - [x] E36. The release notes name the commit and install the exact version
  - [x] E37. Propose the next version
  - [x] E38. The Release workflow starts from a button
  - [x] E39. Support every newer Shikiru, with a minimum version only
  - [x] E40. git-cliff writes the notes and the version
  - [x] E41. A daily release marked Latest
  - [x] E42. A resilient daily release
  - [x] E43. Delete record_telegram.py
  - [x] E44. The contract check runs in CI
  - [x] E45. The contract check is a job of CI; scripts live in .github/
  - [x] E46. The contract job runs with a tmux that Shikiru supports
  - [x] E47. A command no longer fails on macOS when its process group has just exited
  - [x] E48. Check the Shikiru contract every day at 01:00 UTC
  - [x] E49. The daily contract check runs only after a Shikiru release
