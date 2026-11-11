# Shikiri-yaku reference

This is the full reference for Shikiri-yaku. The [README](../README.md) has the overview and quick start.

## Configuration

`config.toml` accepts only the sections and keys in this reference. An unknown or misspelled key is an error.

- **Required:** `product`, one or more `[[components]]` entries (each with `name` and `path`), and `[channels.telegram]` with `owners`. The Telegram section is required even when every other value is the default.
- **Names** start with an ASCII letter or digit. They may then contain letters, digits, `_` or `-`. Component names must be unique.
- **Components run on this machine.** `path` must be absolute or start with `~`. Remote `host` components are not supported.
- **Removed components:** their open signals are closed, with a note, at the next start.
- **`token_file`** defaults to `secrets/telegram.token`, relative to the instance directory. It may be another relative path there, or an absolute path.
- **`group_id`,** when set, must identify a Telegram forum supergroup.

### `[channels.telegram]`

| Key | Default |
| --- | --- |
| `owners` | required; one or more integer IDs |
| `token_file` | `"secrets/telegram.token"` |
| `group_id` | none |
| `allow_other_members` | `false` |
| `shared_group` | `false`; requires `group_id` |

### `[operator]`

All keys are optional. Every value is a positive integer. `telegram_call_timeout_s` must be greater than `telegram_poll_wait_s`.

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

### `[handlers.infrastructure]`

All keys are optional. Every value is a positive integer.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `command_timeout_min` | 10 | `quota_wait_max_h` | 24 |
| `quota_wait_margin_min` | 1 | `retry_budget_per_day` | 1 |
| `restart_budget_per_hour` | 1 | `restart_confirm_s` | 60 |
| `restart_poll_s` | 5 | `approval_expiry_h` | 24 |
| `host_lock_poll_s` | 1 | `install_output_tail_lines` | 20 |

### `[handlers.product]`

All keys are optional. Every value is a positive integer.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `reminder_first_h` | 4 | `reminder_repeat_h` | 24 |
| `message_title_max_chars` | 80 | `button_label_max_chars` | 30 |

### `[reasoner]`

The section is optional. Without it, the Reasoner is off, and escalations that need a model assessment go to the Product Owner.

```toml
[reasoner]
ladder = ["claude:<model>:<effort>", "codex:<model>"]
```

Replace the placeholders with model and effort values that the installed CLI accepts.

- `ladder` is required when the section is present. It lists one or more rungs in fallback order. Each rung is `adapter:model` or `adapter:model:effort`.
- The adapter must be `claude` or `codex`. The model and effort must be non-empty strings. Effort is optional. Claude receives it. Codex ignores it.
- The local CLI for every rung must be installed and runnable.
- The numeric settings are optional positive integers.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `timeout_s` | 120 | `max_parallel` | 2 |
| `cooldown_min` | 5 | `input_max_chars` | 4000 |

## Command line

```
shikiri-yaku [--product NAME] [--home PATH] [--json] [--version] COMMAND
```

Global options go before the command.

| Option | Meaning |
| --- | --- |
| `--product NAME` | The product to work on. You may leave it out when `--home` holds exactly one product directory. With none or several, the command refuses and names what it found. |
| `--home PATH` | The directory that holds the product directories. Default: `~/.shikiri-yaku`. |
| `--json` | Prints `doctor` and `stats` as one JSON object, and each `service` command as a JSON list. Other commands ignore it. Exit codes stay the same. |
| `--version` | Shows the version and exits. |

| Command | Purpose |
| --- | --- |
| `run` | Serves one product until SIGTERM. |
| `doctor` | Checks one product's instance. |
| `stats` | Prints the figures of one product's journal. |
| `service install` | Writes and starts a launchd or systemd unit that runs `run` at login. |
| `service start` | Starts one product's service. Without `--product`, it starts every product under `--home`, in name order. Starting a running service leaves it running. |
| `service stop` | Stops one product's service and waits until it has stopped. Without `--product`, it stops every product under `--home`, in name order. A service still running 30 seconds after the stop is a failure. |
| `service restart` | Restarts one product's service. Without `--product`, it restarts every product under `--home`, in name order. |
| `service uninstall` | Stops one product's service, keeps it from starting at login and deletes its unit file. Without `--product`, it does so for every product under `--home`, in name order. A product whose unit was never installed is `not installed` and passes. |

The `service` commands other than `install` do not refuse several products. Each prints one line per product and exits 1 if any product failed.
| `telegram adopt <component> <thread-id>` | Repairs an uncertain topic. See [Topic recovery](#topic-recovery). |
| `telegram forget <component>` | Deletes an uncertain topic's row. See [Topic recovery](#topic-recovery). |

The older forms, with the product after the verb (`run shop`) or `topics …`, are usage errors.

When `stats` cannot run, it prints one `✘` line on standard error and nothing on standard output. `doctor` reports its failed checks inside the JSON object.

### JSON output

| Command | Keys |
| --- | --- |
| `doctor` | `passed` (true when every check passed), `failed` (the number of failed checks), `total`, and `checks`. Each entry of `checks` has `name`, `passed` and `detail`. |
| `stats` | `completed`, `queued`, `lifetime_seconds`, `waiting_seconds`, `share`, `handled_without_po`, `reasons`, `verdicts`, `installs` and `install_outcomes`. The durations are in seconds. `share` is the waiting fraction from 0 to 1, or `null` when unavailable. The last five keys each map a label to its count. |
| `service start`, `service stop`, `service restart`, `service uninstall` | A list with one entry per product, in name order. Each entry has `product`, `passed` (true when the action finished) and `result`: `started`, `stopped`, `restarted` or `uninstalled`, or the reason it did not, such as `not installed`. |

## Telegram

Only the Telegram user IDs listed in `channels.telegram.owners` can issue commands or act on buttons. Several owners are allowed.

- **Private chat:** each owner must open the bot and press **Start** first.
- **Forum group:** set `group_id` to the numeric ID of a forum supergroup.
  - The bot must be an administrator with permission to manage topics.
  - Every owner must be a member of the group.
  - By default, the group must hold only the bot and the owners, because other members can read its messages.
  - `allow_other_members = true` allows more readers. Only owners can still act. Messages and button taps from non-owners are ignored.

### Several instances in one group

Several instances can share one forum group. Each instance needs its own bot, token file and components. Two instances never share a bot.

Give every instance the same `group_id`, and set `shared_group = true` in each `[channels.telegram]` table. `shared_group` requires `group_id`. With it:

- A command (text that starts with `/`) in a topic that is neither General nor this instance's component topic gets no reply from this instance.
- `/goal <component> <request>` in General is submitted only when `<component>` belongs to this instance. For another instance's component, it gets no reply.
- `/status` and `/open` in General cover only this instance's components.
- `/help`, free text, and a `/goal` without a component are answered by every instance's bot. The group receives one list per instance.

Every instance's bot must be an administrator of the group, with the manage-topics right. Start-up reads the administrators, including the other bots. It admits the owners, this bot, and the other administrator bots. It refuses any other member, and any bot that is not an administrator. To admit an instance's bot, make it an administrator.

`allow_other_members = true` does not admit another instance's bot. It skips the membership check, so any member can read the group.

### Topic recovery

With a forum configured, start-up creates a topic for each component. It closes the topics of components removed from the config. The General topic is used for Operator messages. The Operator never creates or adopts it. Topic changes are recorded in the journal. The Bot API does not let the Operator list existing forum topics.

If start-up reports an uncertain topic creation, check the forum yourself. Then choose one case:

1. **The topic exists.** Adopt its positive numeric thread ID, the number at the end of the topic link:

   ```sh
   shikiri-yaku --product shop telegram adopt api 123
   ```

   Adoption records the thread. It does not check that the ID belongs to a real topic.

2. **No topic was created.** Forget the uncertain row, then start the service again to create the topic:

   ```sh
   shikiri-yaku --product shop telegram forget api
   ```

`forget` removes only the uncertain row. It does not delete a topic that may exist. Telegram allows duplicate topic names, so forgetting a row can leave an orphan topic and create another one. A fresh `--home`, or a deleted journal, also loses the topic mapping. Do not point a new instance at the same group without adopting its existing topics.

## Chat commands

Commands work in private chats and in forum topics. Replies go to the chat and topic where the command was sent.

| Command | What it does |
| --- | --- |
| `/status` | Shows each component's engine state, running, queued and escalated task counts, quota blocks, and Reasoner rung health. In a component topic, it shows only that component, without the Reasoner rungs. |
| `/open` | Re-shows open questions and pending approvals that Shikiru still reports as open. Each item goes to its component's destination. If none are waiting, the bot says so in the command's chat. In a component topic, it re-shows only that component's items. |
| `/help` | Lists the supported commands. Free text also returns this list. An unknown slash command reports its name and lists the commands. |
| `/goal <component> <request>` | Submits a task request to that component's Shikiru project. Use this form in a private chat or the General topic. |

### `/goal`

The request goes to Shikiru as written. It is not split across components or rewritten into a plan. In a component topic, the topic names the component, so write `/goal <request>` there:

```text
/goal api Add CSV export to the reports page
```

### Goal outcomes

The outcome depends on what Shikiru reports:

- **Confirmed:** the bot replies with the task ID.
- **Rejected:** the bot says the goal was not submitted.
- **Connection failed after submission may have reached Shikiru:** the bot says the result is uncertain and offers **Submit again**. It never retries on its own.
- **Crash after submission, before the acknowledgement was recorded:** after a restart, the same uncertain prompt with **Submit again** appears. Recovery marks the goal uncertain and does not send it again. Check the Shikiru project before you tap **Submit again**. The first request may have been accepted, so a retry can create a duplicate task.

## Questions, approvals and reminders

An open escalation arrives with its question and the choices Shikiru offers. Tap a choice to apply it.

- **Text answers:** a choice that asks for text prompts a reply to the bot's message. Reply in the same chat and topic.
- **Confirmation:** **Accept** and **Cancel task** need a second **Confirm** tap before Shikiru receives the choice.
- **Show again:** `/open` re-shows open questions.
- **Details:** when a question is shortened to fit Telegram's message limit, it includes **Details**. That button requests the escalation packet from Shikiru and sends it in more messages. Details can contain project data. A packet that matches a secret pattern is refused and not sent.

### Reminders

- Open questions are reminded after 4 hours, then every 24 hours. Change these in `[handlers.product]`.
- After downtime, the scheduler sends only the latest due reminder, not a backlog.
- Standing notices, such as an engine that stays down, have **Stop reminders**. Tapping it ends the reminders and edits the notice to **Reminders stopped**. It does not close the underlying signal.
- When the issue is fixed, the bot edits the notice to its final state.

### Install approval

An install approval shows the tool, a reason written by the model, the complete command, its package manager, and the possible effect.

The model cannot supply a command. The code builds the argument list from one of these recipes, stores it before asking, and checks that the stored list still matches the displayed recipe before running it:

| Recipe, then the validated package name | Package manager |
| --- | --- |
| `brew install` | Homebrew |
| `uv tool install` | uv |
| `npm install -g` | npm |
| `pipx install` | pipx |
| `cargo install` | Cargo |

- **Install:** tap only when you approve that exact command and its effect. The command runs on the Operator host, outside the component repositories, and only after the approval is recorded. It may update the dependencies of the installed tool.
- **Decline:** runs nothing and forwards the original escalation to you.
- **Expiry:** an approval expires after 24 hours by default (`approval_expiry_h`). `/open` shows only approvals that are still valid and open.

## Automatic actions

The Operator acts on its own only in the cases below. Anything else reaches you as a question, with what the Operator tried.

- **Agent and integration errors** (`agent_error`, `agent_stuck`, `agent_protocol`, `integration_conflict`, `integration_blocked`): the escalation goes back to Shikiru's agents at once. Each cause gets `retry_budget_per_day` retries a day, one by default. After that, the escalation is forwarded to you.
- **Quota exhausted** (`quota_exhausted`, `ladder_exhausted`): the bot tells you when the quota resets, then retries after that time plus `quota_wait_margin_min`. A reset that is unknown, or further away than `quota_wait_max_h` (24 hours by default), is forwarded at once.
- **Engine down or faulted** (`engine_down`, `engine_fault`): the bot asks Shikiru to start the engine, waits until it is up, and tells you. If the start is refused, fails or is not confirmed, the bot forwards the failed `shikiru doctor` checks and asks you to run `shikiru start` in that project. An `engine_error` escalation gets the same restart if the engine is down, and is then retried. Restarts are limited to `restart_budget_per_hour` per component, one an hour by default.
- **Missing tool:** an install runs only after you tap **Install**. See [Install approval](#install-approval).
- **Pull request ready for review:** when Shikiru reports an open pull request for a task, the bot sends its title, its commit count and an **Open PR** link, with **Stop reminders**. When the pull request is merged, closed unmerged or withdrawn, the message is edited to a final line that says how it ended. The Operator never merges, comments on or reviews a pull request.
- **Finished work:** when a task and its child tasks have all settled, the bot sends one notice per task family, such as "api · Export invoices integrated (3 tasks)". It has no buttons.
- **Health:** when every Reasoner rung fails, decisions are forwarded without a suggestion. When a component's Shikiru link stays unreachable for `link_unreachable_min` (10 minutes by default), the bot says so. Either notice has **Stop reminders** and is edited to "Recovered" when the problem clears.

Standing messages (the pull-request message and the health notices) keep their **Stop reminders** button. It silences their reminders without closing the underlying signal.

## Service

Run `shikiri-yaku --product shop service install` after you install the command and create a valid instance. It writes a per-product service unit under your home directory, prints the unit's path and starts the service. Reinstalling replaces the running service with the new unit.

Stop any other process holding the instance lock, such as a foreground `run`, first. Otherwise the service cannot acquire the lock and exits. The unit carries the `PATH` of the shell where `service install` ran, so `shikiru` and the configured Reasoner CLIs can be found when the service starts.

### macOS

The unit is a LaunchAgent at `~/Library/LaunchAgents/shikiri-yaku-shop.plist`. Start it and stop it with:

```sh
shikiri-yaku --product shop service start
shikiri-yaku --product shop service stop
```

`service stop` sends SIGTERM and returns once the job no longer runs. The job stays loaded, so `service start` works again without another setup step. To check whether it runs:

```sh
shikiri-yaku --product shop service status
```

- The generated plist sets no stdout or stderr log files. Use `service status` to inspect the job state. To see application output while you troubleshoot, stop the service and run `shikiri-yaku --product shop run` in a terminal.

### Linux

The unit is a user service at `~/.config/systemd/user/shikiri-yaku-shop.service`. Start it and stop it with:

```sh
shikiri-yaku --product shop service start
shikiri-yaku --product shop service stop
```

To check whether it runs, and to read its log:

```sh
shikiri-yaku --product shop service status
journalctl --user -u shikiri-yaku-shop.service -f
```


### Clean stops and rewritten units

- `run` handles SIGINT and SIGTERM as clean stops and exits successfully.
- The generated units restart after an unsuccessful exit. A stop requested through the service manager is not restarted.
- Only one process can hold an instance's lock.
- After you change `config.toml`, restart the product's service so it reads the new settings.
- `service install` reloads a rewritten unit and replaces the running service.

## Applying changes

A running service reads its code and `config.toml` only when it starts. A change takes effect after a restart.

`shikiri-yaku service restart` restarts the unit of each product under `--home`, or of the one named by `--product`. On macOS it runs `launchctl kickstart -k`. On Linux it runs `systemctl --user restart`. `service start`, `service stop` and `service status` act the same way, with their own commands: start does not replace a running job, stop waits on macOS until the job has stopped, and status reports `running`, `stopped`, `failed` or `not loaded`, and exits 1 unless the service runs. A product whose unit was never installed is reported as `not installed`, and nothing runs for it. An action that the service manager refuses, such as a unit that was written but not loaded, is reported with the manager's message.

`shikiri-yaku --product shop service uninstall` is the reverse of `service install`. It stops the service and keeps it from starting at a later login: on macOS it runs `launchctl bootout`, and on Linux it runs `systemctl --user disable --now` and then `systemctl --user daemon-reload` after the unit file is deleted. It then deletes the unit file. A manager answer that only says the unit is not loaded, not enabled or not running is not a failure. If the manager refuses the stop, or the file cannot be deleted, the result says why and the unit is kept or reported with its path. It keeps `config.toml`, the token file, the journal and its backups. A `run` process started in a terminal is not touched.

The service runs the installed copy of `shikiri-yaku` that `service install` found on the login `PATH`. It never runs a checkout. That copy is a release. Do not install with `--editable`. The running service would follow unreviewed checkout changes, including after a crash restart.

### After a code change

1. The owner releases it by running the Release workflow, as [CONTRIBUTING.md](../CONTRIBUTING.md#releasing) describes.
2. On each host, upgrade the installed copy and restart its services:

   ```sh
   uv tool upgrade shikiri-yaku
   shikiri-yaku service restart
   ```

Before a release migrates a journal, `shikiri-yaku` copies it to `<home>/<product>/backups/journal-schema<N>.db`. `<N>` is the schema version before the migration. The newest three copies are kept per product.

### After a `config.toml` change

Run `shikiri-yaku service restart` alone.

### Rolling back

To go back to an earlier release, install its version and restart:

```sh
uv tool install shikiri-yaku==<version>
shikiri-yaku service restart
```

A rollback restores code, not data. A release that migrated a journal leaves the old version unable to open it. It stops with `JournalTooNewError`. To restore the data, repeat these steps for that product on each host where its service runs:

1. Stop the service: `shikiri-yaku --product <product> service stop`.
2. Copy the newest `journal-schema<N>.db` in `<home>/<product>/backups/` over `<home>/<product>/journal.db`.
3. Delete `journal.db-wal` and `journal.db-shm` if they exist.
4. Start the service again: `shikiri-yaku --product <product> service start`.

Changes made after that backup are lost.

## Recovery

### Doctor and stats

- `shikiri-yaku --product shop doctor` checks the configuration and component paths, the instance lock, the journal, Telegram readiness, each Shikiru MCP link, and each configured Reasoner CLI. Run it after setup or a configuration change. It may contact Telegram and Shikiru. A failed check gives exit code 1.
- For machine-readable output, put `--json` before the command: `shikiri-yaku --json --product shop doctor`.
- `stats` reads the local journal and summarizes completed and queued work, waits, reasons, verdicts and install outcomes. It does not contact Shikiru.

### Foreground diagnostics

Stop the service first, then run `shikiri-yaku --product shop run`. The service and the foreground command use the same instance lock, so a second `run` is refused.

### Restart recovery

The Operator keeps its journal across restarts. It reconciles interrupted work with what Shikiru and the host report.

- **Resolve:** an issued resolve may have reached Shikiru. After a restart, the Operator checks whether the escalation is still open. Then it confirms the result, or shows the question again.
- **Install approved, not issued:** it resumes after a restart if the escalation is still open. The stored, already-approved command runs.
- **Install issued:** it never runs a second time. Recovery checks whether the tool exists. If it does, Shikiru is answered. If it is missing, the bot asks for a fresh approval.
- **Duplicate messages:** Telegram may show one duplicate if the process stopped after Telegram accepted a send but before the journal recorded the acknowledgement.

### Engine-down signals and clean stops

- An unexpected engine-down signal can trigger one automatic restart per component per hour by default.
- If a start fails or the budget is used up, the bot forwards the reason and the failed `shikiru doctor` checks. It tells you to run `shikiru start` in that component's project.
- A clean Shikiru engine stop, seen while the Operator is running, is treated as intentional after `stop_grace_min` (2 minutes by default).
- If the Operator restarts before that grace period is recorded, a stop not yet recorded as respected may be treated as an engine-down signal on the next run.
- A stop recorded as respected stays respected across Operator restarts. During a first sync, a clean stop found in Shikiru's history also counts as respected.
- `/status` shows the current engine state.

## Local state

Each product's state lives under `<home>/<product>`. The default is `~/.shikiri-yaku/<product>`.

- `config.toml` and the configured token file.
- `journal.db`: signals, outbound message bodies, goal text, pending operations, and restart and recovery state.
- `instance.lock` while the product runs. The shared `<home>/host.lock` is held while any instance runs a host command.
- `install-scratch/`: approved install commands, outside all component repositories.
- `output/install-<op-id>.log`: install output. The directory has mode `0700` and each file has mode `0600`, because package-manager output can contain sensitive paths or tokens.
- `reasoner-scratch/`: temporary directories for Reasoner runs.
- `backups/journal-schema<N>.db`: journal copies made before migrations. See [Applying changes](#applying-changes).

Keep the instance directory private. Back it up with the same care as other operational data.

## Data that leaves the machine

- The bot sends Shikiru's question or headline, the offered choices and their consequences, relevant task and component names, and handler notes to Telegram. **Details** sends more of the Shikiru packet when you request it.
- Telegram group members can read posted messages. This includes non-owners when `allow_other_members = true`. Only owners can act.
- When the Reasoner is enabled, it receives the escalation details and host facts it needs. The CLI or provider handles those inputs.

## Limits

- Components must be local Shikiru project directories. Telegram is the only chat channel.
- The Reasoner is optional. It uses the configured local Claude or Codex CLIs. Without one, escalations that need a model assessment go to the Product Owner.
- If every configured Reasoner rung fails, the escalation still goes to the Product Owner, with a note and a Reasoner health signal.
- A Claude rung gets no file-reading tool, only the web and package lookups its adapter lists. A Codex rung's read-only sandbox stops writes but not reads. Its model can read any file your user account can read.
- Automatic quota waits are limited to 24 hours by default (`quota_wait_max_h`). Retries are limited to one per day (`retry_budget_per_day`). Engine restarts are limited to one per component per hour (`restart_budget_per_hour`). You can change these values in the configuration.
