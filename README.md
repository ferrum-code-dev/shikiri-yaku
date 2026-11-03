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

## Status

Design stage, no code yet. The design is in [docs/design.md](docs/design.md). The effort estimate
and the plan to build it with Shikiru itself are in [docs/plan.md](docs/plan.md).

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
- `telegram adopt <component|general> <thread-id>` and `telegram forget <component|general>`
  repair an uncertain Telegram topic, or reuse one made by hand, as described below.
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

With a group that has forum topics, the Operator creates one topic per component and a General
topic at start-up, but only when its own journal has no open topic for that component. It never
looks at the forum, because the Telegram Bot API cannot list a forum's topics. Telegram accepts
duplicate topic names, so a fresh `--home`, a deleted journal or
`shikiri-yaku --product <product> telegram forget <component|general>` makes the next start create
a second topic with the same name. A topic created by hand is never adopted automatically. To reuse
one, run `shikiri-yaku --product <product> telegram adopt <component|general> <thread-id>` before the
first start; the thread id is the number at the end of the topic's link. If a start-up is
interrupted while a topic is being created, the next start refuses and names `telegram adopt` and
`telegram forget`.
