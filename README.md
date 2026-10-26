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
- **Agents propose, code acts.** The Operator's reasoning agent runs with tools disabled; only
  plain code runs commands, and only ones the owner approved.
- **Shikiru stays the source of truth,** read and commanded through MCP. Nothing of the Operator
  is stored in the managed repositories.
- **One instance per product,** with its own chat bot. Each component is a Shikiru project.
- **Small by design:** one Python process, SQLite, one TOML file and the OS service manager.

## Status

Design stage, no code yet. The design is in [docs/design.md](docs/design.md). The effort estimate
and the plan to build it with Shikiru itself are in [docs/plan.md](docs/plan.md).
