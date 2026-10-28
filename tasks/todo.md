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
- [ ] A8d. Source cleanup and failing sources
- [ ] A9. Product handler
- [ ] A10. Telegram send and edit
- [ ] A11. Telegram inbound and replies
- [ ] A12. Command confirmation and resolved elsewhere
- [ ] A13. Status, open, help and measures
- [ ] A14. `/goal`

## Later

- [ ] Phase B: Topics
- [ ] Phase C: Infrastructure basics and review
- [ ] Phase D: Install (decide after the gate in `docs/plan.md` §2.3)
- [ ] Phase E: Hardening
