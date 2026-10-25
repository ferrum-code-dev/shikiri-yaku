# shikiri-yaku

An autonomous Operator for product development with Shikiru.

Shikiri-yaku is a small background service that sits between a Product Owner and [Shikiru](https://github.com/ferrum-code-dev/shikiru), the
coordinator that runs coding agents. The Product Owner describes outcomes and makes product
decisions in a chat (Telegram first, other channels later). Shikiri-yaku takes responsibility for
getting the technical work done: it briefs Shikiru, watches its events, answers the escalations it
is entitled to answer, repairs the development machine when work stalls, and reports progress
honestly as implemented, verified, integrated or released.

- **One instance per product.** A product has one or more components, each a Shikiru project
  (repository). Several products mean several instances, each with its own chat bot.
- **Only product questions reach the owner,** each with consequences, a recommendation and
  buttons. An autonomy dial lets routine choices be taken without waiting, and it grows only on
  track record.
- **Shikiru stays the source of truth.** Shikiri-yaku reads and commands it through MCP and keeps
  only what Shikiru cannot: the conversation, goals across components, a small model of the
  product's components, decisions, incidents and an intent journal for restart recovery. Nothing
  of the Operator is stored in the managed repositories. What Shikiru lacks for this is added to
  Shikiru as a requirement, not worked around in the Operator.
- **Agents are replaceable,** on subscription or API access, and all model use goes through
  Shikiru, so usage, quota and ladders stay in one place. Reasoning agents investigate and
  propose; they never hold host rights.
- **Small by design:** one Python process, SQLite, one TOML file and the OS service manager.

## Status

Design stage, no code yet. The design, with the nine scenarios, the remembered state and the
required Shikiru changes, is in [docs/design.md](docs/design.md). The effort estimate and the plan
to build it with Shikiru itself are in [docs/plan.md](docs/plan.md).
