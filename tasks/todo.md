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
  - [x] D1. Reasoner spike (`tasks/reasoner-spike.md`)
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
- [ ] Phase E: Hardening
  - [x] E1. Recovery of an uncertain topic creation
  - [x] E2. Drills as tests
  - [x] E3. Recording real Telegram replies
  - [x] E4. Install path hardening from the security review
