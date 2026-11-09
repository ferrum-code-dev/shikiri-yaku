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
- [x] A4b. Install service (install-service writes the launchd plist or systemd unit; doctor shows a held lock as running)
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
  - [x] E17. `restart` verb and `poe deploy`
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
