# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Below 1.0, any minor version may change
behavior, the configuration or the journal schema, and the entry says so when it does.

## [Unreleased]

## [0.1.3] - 2026-11-07

### Changed

- The README has Release, License and CI badges and the Japanese name in its title.
- The `--help` test no longer depends on the CI environment. This is a test change only.

## [0.1.2] - 2026-11-07

### Fixed

- `poe deploy` waits for a freshly restarted service: it retries `doctor` for each product for up
  to a minute before rolling back, and a rollback report now includes `doctor`'s output.

## [0.1.1] - 2026-11-07

### Fixed

- `doctor` names the cause of a failed link to a component. A path that is not a Shikiru project
  now says to run `shikiru init` there, in place of "unhandled errors in a TaskGroup".

### Changed

- The README installs a release, from its wheel or its tag, instead of a checkout.

## [0.1.0] - 2026-11-07

### Added

- Watches the escalations of every Shikiru project of a product, sorts each into a category and
  hands it to that category's handler.
- Automatic handling: retries of transient agent errors, waits for a quota reset (up to 24 hours by
  default), restarts of stopped engines, and installs of missing user-level tools after the owner
  approves the exact command.
- Forwarding to the Product Owner in Telegram, where one tap or a short reply applies the answer in
  Shikiru. Groups with one forum topic per component are supported.
- Chat commands `/goal`, `/status`, `/open` and `/help`, in private chats and in topics.
- Review messages for pull requests, and notices for finished task families and for health.
- An optional Reasoner: local Claude or Codex CLIs, tried in order. Without one, escalations that
  need a model assessment go to the Product Owner.
- Command line `shikiri-yaku` with `run`, `doctor`, `stats`, `restart`, `install-service` for
  launchd and systemd user units, and `telegram adopt` and `telegram forget`; `--json` output for
  `doctor` and `stats`.
- Supports Shikiru from 0.1.0 up to, but not including, 0.2.0; `doctor` reports a version outside
  that range.
