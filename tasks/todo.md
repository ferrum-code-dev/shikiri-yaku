# Todo

Details and acceptance criteria are in `tasks/plan.md`. Mark a task `[x]` only when its change is
merged and `uv run poe check` passes.

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
- [ ] A14. `/goal`

## Later

- [ ] Phase B: Topics
- [ ] Phase C: Infrastructure basics and review
- [ ] Phase D: Install (decide after the gate in `docs/plan.md` §2.3)
- [ ] Phase E: Hardening
