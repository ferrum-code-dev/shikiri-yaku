# Expert: proposed design text

This file holds the text proposed for `docs/design.md` and the facts the spike of 2026-11-11
established. Task X1 moves the text into the design and deletes this file. Until then, where this
file and `docs/design.md` differ about the Expert, this file wins.

## Text for the design

### Purpose (new paragraph, after the list of handlers)

The Operator also answers the PO's own questions about the product. The **Expert** is a read-only
agent the PO talks to in the chat: it reads the repositories of the components, looks things up on
the web within what the PO has granted, and answers at product level. When an idea needs work, it
proposes one task per component, and the PO submits them with one tap. The Expert removes the
planning a PO would otherwise do by hand before a goal that spans several components.

### Principles (changes)

* Principle 4 gains one sentence: "The Expert follows the same rule: it reads and proposes, and
  code submits the tasks the PO approved."
* Principle 6 gains one sentence: "With every model down `/ask` says so, and `/goal` still works."

### Modules (new rows)

| Module | Responsibility | Uses a model? |
|---|---|---|
| **Expert** (`app/expert.py`, `app/expert_ladder.py`, `adapters/expert_cli.py`) | Runs the turns of each conversation, one at a time, through a ladder of agent CLIs ([Expert](#expert)) | Yes |
| **Web tool** (`adapters/expert_web.py`) | The only way an Expert agent reaches the web: an MCP tool that serves granted domains and records requests for the others | No |

### Expert (new section, after Reasoner)

The Expert is not an extension: extensions handle signals, and a PO message is not a signal. It is
a module on the inbound side with its own ladder, configured in `[expert]`. Without that section,
or without `channels.telegram.group_id`, `/ask` is refused with the reason.

**Conversation.** `/ask <text>` opens a conversation. Asked in a component topic, its scope is that
component; asked in General, its scope is every component. The Operator creates a forum topic for
the conversation, with the Expert's icon and a name cut from the question, and answers the command
with a button that opens the topic. The topic is renamed when the first answer brings a title.
Every message an owner sends in that topic is a **turn**. Turns of one conversation run one at a
time, oldest first; conversations run in parallel up to `expert.max_parallel`. `/ask` is journaled
by its Telegram update id before the topic is created, so a duplicate delivery opens nothing twice.

**Topic commands.** In an Expert topic only these commands exist: `/effort` shows the effort of the
conversation, `/effort <level>` changes it from the next turn, `/summarize` asks for a very short
summary, `/close` deletes the conversation, and `/help` lists these commands. Any other command is
answered with that help.

**Answer.** An agent returns typed data, checked against a schema: `reply`, `title`, `tasks` (each
with `component`, `title` and `text`) and `findings`. The reply is written for a Product Owner: at
product level, without code, short. It is sent as one message and cut to fit; there is no *Details*
button. A task that names a component outside the scope makes the answer invalid.

**Tasks.** An answer with tasks shows one line per task (component and title) and two buttons,
*Submit all* and *Reject all*. *Submit all* submits each task's text with `submit_task`, one
`submit` op per task, in the order given, and the message is edited to show the task ids. The
rules of `/goal` apply: an op left unknown by a crash is never resubmitted, and the PO is asked to
check. To change the proposal, the PO says so in the conversation and gets a new one. Tasks are
submitted together and Shikiru runs them independently, so each task's text carries what it needs
from the others.

**Ladder and session.** `expert.ladder` lists rungs like the Reasoner's. A conversation stays on
the rung that answered last and moves down the ladder only when that rung fails; a failed rung is
skipped for `expert.cooldown_min`. While the provider stays the same, the agent's own session is
resumed, so nothing is read twice, also when the model or the effort changes. When the provider
changes, a new session starts from a **handoff**: the `findings` of the last answer (the agent's
own running notes on what matters and what it concluded, never shown to the PO) and the last
`expert.handoff_turns` turns. The journal keeps every turn, so no switch loses the conversation.
The effort set with `/effort` replaces the effort of every rung for that conversation. When no
rung answers, the topic gets a message saying so.

**What an agent may do.** It reads files and nothing else: no writes, no shell network, no web
tool of its own, no Shikiru tools. Code gives it the open tasks and escalations of the components
in scope as input. Each adapter passes a fixed argument list:

* Claude: `--restricted`, `--tools "Read,Grep,Glob"`, `--permission-mode dontAsk`,
  `--setting-sources ""`, `--strict-mcp-config`, and `--add-dir` for each component in scope. File
  reads outside those directories are refused by the CLI, so the scope is enforced.
* Codex: `--ignore-user-config`, `--disable apps`, `web_search="disabled"`, the read-only sandbox,
  and the web tool pre-approved. Codex can read any file the user can, so for a Codex rung the
  scope is an instruction, not a boundary.

Repository text and web pages are data, never instructions; the prompt says so, and the output is
validated.

**Web.** The only web access is the Operator's MCP tool `web_fetch(url)`, started by the agent CLI
as a subprocess. It serves a page only when its host is in the grants of the journal; a redirect to
another host is checked the same way. For any other host it records a request and tells the agent
that the PO has been asked. After the turn, each requested host is shown to the PO with *Allow*
and *Deny*. *Allow* stores the grant for good, for every conversation; either answer becomes a
turn that tells the agent, so the conversation continues by itself. There is no search tool: a
search site is one more host to grant. Because the check is code, it is the same for every agent.

**End of a conversation.** A conversation ends when the PO closes its topic, sends `/close`, or
sends nothing for `expert.retention_days` (14). The Operator then deletes the topic, the
conversation's rows and its directory. Telegram does not tell a bot that a topic was deleted, so a
topic the PO deleted is cleaned up when the next send fails or when it expires.

**Restart.** A turn that was running is marked interrupted and the PO is told in the topic; it is
not run again by itself. A topic whose creation is uncertain is not adopted: the PO is told to ask
again.

### PO channel (changes)

* Commands: add `/ask`. In a shared group, `/ask` in General must name the bot (`/ask@<bot>`),
  because every instance reads General; without the name it is ignored.
* Forum topics: Expert topics are a second kind of topic beside component topics. Deleting one
  needs the bot right `can_delete_messages`; the start-up check requires it when `[expert]` is set.

### Journal (changes)

New tables: `conversations` (number, scope, topic, effort, provider and session id, findings,
state, last activity), `turns` (conversation, Telegram update id, text, answer, rung, state, call
record), `web_grants` (host, granted at) and `web_requests` (conversation, turn, host, state).
`topics` also holds Expert topics. Replace "There is no retention job" with: "The only retention
job deletes ended Expert conversations."

### Known limits (changes)

* Limit 2 becomes: "**Models run outside Shikiru.** The Reasoner makes a few cheap calls. The
  Expert's calls are neither few nor cheap: they read repositories. Both are logged by the
  Operator, not in Shikiru's ledger."
* Limit 6 becomes: "**Tasks of one plan are submitted together.** The Expert plans across
  components, but nothing orders the tasks: Shikiru runs them independently."
* New: "**A Codex rung is not confined to the scope.** It can read any file the user can. It has no
  way to send what it reads anywhere except to a granted host and to the PO's chat."
* New: "**A granted host is trusted.** A page of a granted host can carry text that steers the
  agent, and a request to a granted host can carry data out in its URL."
* New: "**Agent sessions stay in the agent's own store.** Deleting a conversation deletes the
  Operator's data; the session files the CLIs keep under their own directories are not touched."

## Spike of 2026-11-11

Run with Claude Code 2.1.322 and codex-cli 0.167.1, a stand-in MCP server and throwaway files.

| Check | Claude | Codex |
|---|---|---|
| Resume with an answer schema | Works: `--resume <id>` with `--json-schema` | Works: `codex exec resume <id>` with `--output-schema` |
| Model change inside a session | Works, context kept | Not tested |
| The MCP web tool, served and refused | Works | Works with `-c 'mcp_servers.<name>.default_tools_approval_mode="approve"'`; without it: "MCP tool call requires approval, but approval policy is never" |
| Own web tools off | With `--tools "Read,Grep,Glob"` | Only with `--ignore-user-config`, `--disable apps` and `-c 'web_search="disabled"'`. With the user config loaded it reached the web through `mcp__codex_apps__search_service_web_run` |
| No writes | Yes | Yes: `touch` fails with "Operation not permitted" |
| Shell network | No shell | Blocked: `curl` exits 6 |
| Reads confined | Yes, with `--restricted`: a read outside `--add-dir` is refused | No |

Other facts:

* `codex exec resume` has no `-s`; the sandbox is passed as `-c 'sandbox_mode="read-only"'`.
* Claude's `--add-dir` takes several values and swallows a prompt given as the last argument. The
  prompt goes through stdin.
* The Claude answer carries `session_id`; Codex prints `thread_id` in its `--json` events.
* Telegram: `deleteForumTopic` needs `can_delete_messages` (read from a summary of the Bot API
  page; confirm on the first real call). The Message object has no field for a deleted topic;
  `forum_topic_closed` exists. A topic icon is a custom emoji from `getForumTopicIconStickers` or
  one of six colours.
* Not tested: real Telegram calls, an effort setting for Codex, a model change inside a Codex
  session.
