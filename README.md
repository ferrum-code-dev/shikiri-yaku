# shikiri-yaku

An autonomous Operator for product development with Shikiru.

Shikiri-yaku is a small background service that sits between a Product Owner and
[Shikiru](https://github.com/ferrum-code-dev/shikiru), the coordinator that runs coding agents.
Shikiru stops and waits for a person when it raises an escalation. Shikiri-yaku takes that seat:
it watches the escalations of every Shikiru project of a product, sorts each into a category and
hands it to a handler. A handler either resolves it (retry, wait for a quota reset, restart an
engine, install a missing tool after the owner approves the exact command) or forwards it to the
Product Owner in a chat (Telegram first), where one tap applies the answer in Shikiru.

- **Small fixed core, growth by extension.** Categories, handlers, actions, reasoning jobs and
  channels are registered extensions; routine decisions, goal intake and digests come later
  without changing the core.
- **Every route ends at the owner.** Anything unknown, unsure or failed is forwarded, so nothing
  is dropped and nothing works worse than plain forwarding.
- **Agents propose, code acts.** The Operator's reasoning agent may look things up but cannot change
  anything, and only names what is missing; plain code builds the command, and runs it only after
  the owner approved it.
- **Shikiru stays the source of truth,** read and commanded through MCP. Nothing of the Operator
  is stored in the managed repositories.
- **One instance per product,** with its own chat bot. Each component is a Shikiru project.
- **Small by design:** one Python process, SQLite, one TOML file and the OS service manager.

## Install and first run

This repository contains the Python implementation and command-line interface. Install it from a
checkout using the steps below.

### Prerequisites

- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- Shikiru installed and available as `shikiru` on `PATH`. Each configured component must be an
  existing local Shikiru project directory; Shikiri-yaku connects with
  `shikiru --project <path> mcp`.
- A Telegram bot token. Create a bot with [BotFather](https://t.me/BotFather) and keep its token
  private. The bot uses long polling, so do not configure a webhook for it.
- Optionally, the local `claude` and/or `codex` CLI for a configured Reasoner ladder.

### Install from source

From a checkout of this repository, install the console command with uv:

```sh
git clone https://github.com/ferrum-code-dev/shikiri-yaku.git
cd shikiri-yaku
uv tool install .
```

This installs the source checkout as the `shikiri-yaku` command; it does not use a published
package release. If you already have a checkout, run `uv tool install .` from its root.

### Create an instance

By default, one product's instance lives at `~/.shikiri-yaku/<product>/`. For a product named
`shop`, create the directory and its config file:

```sh
mkdir -p "$HOME/.shikiri-yaku/shop/secrets"
```

Save the TOML below as `$HOME/.shikiri-yaku/shop/config.toml`. Replace the component path with
the absolute path or `~` path of an existing Shikiru project, and replace the example owner ID
with your numeric Telegram account ID.

```toml
product = "shop"

[[components]]
name = "api"
path = "~/src/shop-api"

[channels.telegram]
token_file = "secrets/telegram.token"
owners = [123456789]
```

Create the token file at `$HOME/.shikiri-yaku/shop/secrets/telegram.token` and put only the bot
token in it. Keep it out of source control. The token file must be a regular file with mode
`0600`; set that exact mode with:

```sh
chmod 600 "$HOME/.shikiri-yaku/shop/secrets/telegram.token"
```

The `owners` list contains numeric Telegram user IDs that may act on messages. For private-chat
delivery, each owner must open the bot and press **Start** before it can message them. You can
include several owner IDs.

To use a forum group instead, add `group_id` under `[channels.telegram]` with the numeric ID of a
forum supergroup. The bot must be an administrator with permission to manage topics, and every
configured owner must belong to the group. By default, the group must contain only the bot and
configured owners because other members can read its messages. Set `allow_other_members = true`
to allow additional readers; only configured owners can act on messages.

To enable the optional Reasoner, add a `[reasoner]` table. Its ordered ladder accepts
`adapter:model` or `adapter:model:effort` entries; the supported adapters are `claude` and
`codex`. Replace the model placeholders with model names accepted by the installed CLI. The
optional effort is passed to Claude; Codex ignores it.

Once the config and protected token file are in place, check the instance and start it in the
foreground:

```sh
shikiri-yaku --product shop doctor
shikiri-yaku --product shop run
```

`doctor` checks the local config and token-file mode, Telegram readiness, each Shikiru project
link, and any configured Reasoner CLIs. It contacts Telegram and Shikiru; `run` stays attached
until stopped.

### Configuration reference

Only the sections and keys listed here are accepted. Misspelled or unknown keys are errors.
Product and component names must start with an ASCII letter or digit and may then contain letters,
digits, `_` or `-`. Component names must be unique. At least one component is required. Components
run on this machine: `path` must be an absolute path or begin with `~`; remote `host` components
are not supported.

| Section | Keys and defaults |
| --- | --- |
| Root | `product` (required); `components` (required, one or more `[[components]]` entries) |
| `[[components]]` | `name` (required), `path` (required); `host` is unsupported |
| `[channels.telegram]` | `owners` (required, one or more integer IDs); `token_file = "secrets/telegram.token"`; `group_id` (optional); `allow_other_members = false` |
| `[operator]` | All keys below are optional; every value is a positive integer |
| `[handlers.infrastructure]` | All keys below are optional; every value is a positive integer |
| `[handlers.product]` | All keys below are optional; every value is a positive integer |
| `[reasoner]` | Optional; `ladder` is required when this section is present |

`token_file` defaults to a path relative to the instance directory. It may be set to another
relative path there or an absolute path. The Telegram table is required even when using defaults,
and `owners` has no default. If `group_id` is set, it must identify a Telegram forum supergroup.

#### `[operator]` defaults

`telegram_call_timeout_s` must be greater than `telegram_poll_wait_s`.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `event_wait_s` | 30 | `liveness_interval_s` | 60 |
| `scheduler_interval_s` | 30 | `outbox_interval_s` | 2 |
| `handler_timeout_s` | 900 | `dispatch_interval_s` | 2 |
| `forward_retry_s` | 60 | `stop_grace_min` | 2 |
| `lost_command_min` | 2 | `link_unreachable_min` | 10 |
| `link_backoff_initial_s` | 1 | `link_backoff_max_s` | 60 |
| `link_call_timeout_s` | 30 | `link_start_timeout_s` | 120 |
| `telegram_call_timeout_s` | 30 | `telegram_poll_wait_s` | 20 |
| `inbound_interval_s` | 1 | `command_name_max_chars` | 32 |

#### `[handlers.infrastructure]` defaults

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `command_timeout_min` | 10 | `quota_wait_max_h` | 24 |
| `quota_wait_margin_min` | 1 | `retry_budget_per_day` | 1 |
| `restart_budget_per_hour` | 1 | `restart_confirm_s` | 60 |
| `restart_poll_s` | 5 | `approval_expiry_h` | 24 |
| `host_lock_poll_s` | 1 | `install_output_tail_lines` | 20 |

#### `[handlers.product]` defaults

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `reminder_first_h` | 4 | `reminder_repeat_h` | 24 |
| `message_title_max_chars` | 80 | `button_label_max_chars` | 30 |

#### Optional `[reasoner]`

Omitting this section disables the Reasoner. When present, `ladder` must contain one or more
rungs in fallback order. Each adapter must be `claude` or `codex`; model and effort must be
non-empty strings. The local CLI for every rung must be installed and runnable. The optional
numeric settings below are positive integers.

| Setting | Default |
| --- | ---: |
| `timeout_s` | 120 |
| `cooldown_min` | 5 |
| `max_parallel` | 2 |
| `input_max_chars` | 4000 |

For example, add this section to `config.toml` and replace its placeholders with CLI-supported
values:

```toml
[reasoner]
ladder = ["claude:<model>:<effort>", "codex:<model>"]
```

The Reasoner is optional; without it, escalations that need a model assessment are forwarded to
the Product Owner.

## Command line

Global options come before the command. `--product` names the product. When `--home` (default
`~/.shikiri-yaku`) holds exactly one product directory, `--product` may be left out; with none or
several, the command refuses and names what it found.

```
shikiri-yaku [--product NAME] [--home PATH] [--json] [--version] COMMAND
```

- `run` serves one product until SIGTERM.
- `doctor` checks one product's instance.
- `stats` prints the figures of one product's journal.
- `install-service` writes the launchd or systemd unit that runs `run` at login.
- `telegram adopt <component> <thread-id>` and `telegram forget <component>` repair an uncertain
  Telegram topic, or reuse one made by hand, as described below.
- `--json` makes `doctor` and `stats` print one JSON object on standard output instead of text.
  The other commands ignore it. Exit codes are the same. `doctor` reports its failed checks inside
  the object. When `stats` cannot run, it prints one `✘` line on standard error and nothing on
  standard output.

The older forms, with the product after the verb (`run shop`) or `topics …`, are usage errors.

`doctor --json` has the keys `passed` (true when every check passed), `failed` (the number of
failed checks), `total` and `checks`. Each entry of `checks` has `name`, `passed` and `detail`.

`stats --json` has the keys `completed`, `queued`, `lifetime_seconds`, `waiting_seconds`,
`share`, `handled_without_po`, `reasons`, `verdicts`, `installs` and `install_outcomes`. The two
durations are in seconds, and `share` is the waiting fraction from 0 to 1, or `null` when it is
unavailable. The last five keys each map a label to its count.

## Telegram topics

With a group that has forum topics, the Operator creates one topic per component at start-up, but
only when its own journal has no open topic for that component. It never looks at the forum,
because the Telegram Bot API cannot list a forum's topics. The forum's built-in General topic needs
no setup: the Operator's own messages go there without a thread id, and the Operator neither
creates nor adopts it. Telegram accepts duplicate topic names, so a fresh `--home`, a deleted
journal or `shikiri-yaku --product <product> telegram forget <component>` makes the next start
create a second topic with the same name. A topic created by hand is never adopted automatically.
To reuse one, run `shikiri-yaku --product <product> telegram adopt <component> <thread-id>` before
the first start; the thread id is the number at the end of the topic's link. If a start-up is
interrupted while a topic is being created, the next start refuses and names `telegram adopt` and
`telegram forget`.
