# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately with GitHub's private
vulnerability reporting: on the repository's **Security** tab, choose **Report a vulnerability**.

## Scope

The following are in scope:

- **The approval boundary.** A host command runs only after the Product Owner approved it, and
  what runs is the argument list stored with that approval.
- **The token file.** The bot token is read only by the Operator and is never logged, journaled
  or committed.
- **Command construction.** Every host command is built from validated names and fixed recipes;
  model output never becomes a shell command.
- **Prompt text from signals.** Escalation text that reaches the Reasoner must not change what
  runs on the host without the Product Owner's approval.

Problems in Shikiru, Telegram, or the agent CLIs themselves belong to those projects.

## Supported versions

Only the latest release receives security fixes.

## Threat model

shikiri-yaku trusts the Telegram owners listed in its configuration to decide what runs on the
host, and nobody else. Group members who are not owners can read posted messages but cannot act on
them. The approval boundary keeps a model from acting on its own: a Reasoner may look things up
but cannot change anything, and Principle 4 of [docs/design.md](docs/design.md) (agents propose,
code acts) means a model never writes a command. Code validates the model's typed fields, builds
each host command from fixed recipes, and runs it only once the owner has approved the stored
argument list.
