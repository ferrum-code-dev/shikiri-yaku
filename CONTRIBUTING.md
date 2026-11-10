# Contributing

Thanks for helping with shikiri-yaku. This file covers how to check a change and how to propose
one. The README is the user guide, and [docs/design.md](docs/design.md) describes the purpose and
architecture.

## Contributor validation

Run the offline quality gate with:

```sh
uv run poe check
```

CI runs this same gate on every push to `main` and on every pull request, on Ubuntu 24.04 and
macOS 15 with Python 3.14 (see [.github/workflows/ci.yml](.github/workflows/ci.yml)).

It uses fakes and local fixtures, not a live Telegram bot or agent CLI. The [Telegram fixture
guide](tests/fixtures/telegram/README.md) explains payload provenance, and the [Shikiru fixture
guide](tests/fixtures/shikiru/README.md) explains its MCP payloads.

Two scripts are run by a human, outside the gate:

- `scripts/contract_check.py --shikiru-checkout <path>` checks the Shikiru behavior the Operator
  relies on, against a real engine and its fake agent in a temporary project. It needs git, tmux
  and a Shikiru checkout.
- `scripts/record_telegram.py` records real Bot API replies from a test bot into
  `tests/fixtures/telegram/recorded/`, with the token and ids replaced, and prints how each differs
  from the fixture of the same name. Use a bot made for this, never the production bot, and stop the
  Operator first.

Human checks that need a real host or Telegram test bot are listed in
[tasks/drills.md](tasks/drills.md) and are outside the offline gate. Run them on a throwaway host,
with the test bot and a test chat.

## Proposing a change

Open an issue first for anything larger than a small fix, so the design can be agreed before the
code is written. A pull request should be one change of about 300 lines or fewer, and its
description should give the gate result and the section of `docs/design.md` it touches. The
templates in `.github/` ask for both.

## Architecture and safety rules

[AGENTS.md](AGENTS.md) holds the architecture rules, the safety rules, the test policy and the
style rules. They apply to people as well as to agents.

## Commit style

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/), for example
`feat: add a restart verb` or `docs: explain the approval boundary`. Keep the subject short and
about the change as a whole.

## Before you open a pull request

A change needs the gate green: `uv run poe check` must pass. Run `uv run poe fix` first to format
the code.

## Releasing

Releases are automatic. The Release workflow runs every day at 00:00 UTC without approval, and it
releases the newest commit of `main` on which CI passed. When no commit since the last release
needs one, the run ends green and releases nothing. To release on demand, open Actions, choose the
Release workflow and choose Run workflow on `main`. Leave `version` empty to release the proposed
version, or enter `<major>.<minor>.<patch>` to release that one.

Commits of type `docs`, `test`, `chore`, `ci`, `build`, `style` and `refactor` are left out of the
notes and do not cause a release by themselves, unless they are breaking (`!` or a `BREAKING CHANGE:`
line).

The [design](docs/design.md#releases-and-deployment) says what happens next. The release workflow
builds the wheel and the sdist with the release version, checks that the installed wheel reports
that version, publishes the files to PyPI, and then creates the GitHub release, which creates the
tag and is marked Latest. The release has no attached files; the package is on PyPI. If a run fails
before PyPI accepts the version, it leaves no tag: fix the cause on `main`, and the next daily run or
a manual run starts clean. If PyPI accepted the files but the GitHub release job failed, the next
daily run or a manual run finishes the release: the upload skips the files PyPI already has, and the
GitHub release is created.

One-time owner setup for publishing: on pypi.org, add a pending publisher for the project
`shikiri-yaku` with owner `ferrum-code-dev`, repository `shikiri-yaku`, workflow `release.yml` and
environment `pypi`. On GitHub, create the environment `pypi` in the repository settings, with no
required reviewer and a deployment branch rule that allows only `main`. The workflow code does not
set these; the owner does.

A host upgrades to the newest release with `uv tool upgrade shikiri-yaku`, then `shikiri-yaku
restart` (see the README, Applying changes).
