Output of `muse exec --json` for the Expert's probes, captured on 2026-11-12 with Muse Code 1.4.15
(`1.4.15-R8339.1`, `/opt/homebrew/bin/muse`). No other Muse version was installed, so no second
version was run. The streams are recorded, not written by hand, but the prompts were written for the
probe, and the failure text of the two credential failures is real output while their inputs are
synthetic.

Each `.jsonl` file is one run's stream, one record per line, as Muse wrote it. Each `.stderr.txt`
file is what Muse printed on stderr for a run that ended before a stream started, or for a killed run.
The redaction: every UUID (session, run, command and request ids) is replaced by a stable placeholder
`00000000-0000-4000-8000-…`, so one session keeps one id across its files; the probe root, the
workspace and the home directory are replaced by `<probe-root>`, `<component>` and `<home>`; other
temporary directories are replaced by `<temp>`; the `allowance` block of `run.usage` (account quota
state) is removed. Nothing else is changed. No token or key is recorded. The probe scripts never opened
the login file; Muse reads it itself when it logs in without `META_API_KEY`.

The synthetic inputs are the prompts (a harmless boundary or memory test, each with a `PREFACE` that
says so), the code words (`HERON-42`, `WREN-7`), the host `probe.example.invalid`, the invalid key
`m2-probe-invalid-key` (set in the environment, not in any file) and the model `m2-probe-missing-model`.

| File | Stands for | Exit code | Last record |
| --- | --- | --- | --- |
| `success-schema-turn1.jsonl` | the first turn of a conversation with a UUID never used before, with `--output-schema`; the answer carries the code word; has `run.usage` | 0 | `run.terminal.completed` |
| `resume-turn2.jsonl` | the second turn, same UUID; the answer gives the first turn's code word | 0 | `run.terminal.completed` |
| `resume-turn3.jsonl` | the third turn, same UUID; repeats the code word and counts the two earlier user turns | 0 | `run.terminal.completed` |
| `interrupted-sigterm.jsonl`, `.stderr.txt` | a turn sent SIGTERM 4 s after start; no terminal record | 143 | `task.lifecycle.status` ("opening meta model stream") |
| `resumed-after-sigterm.jsonl` | the next turn on the same UUID after the kill; recalls the code word from an earlier turn | 0 | `run.terminal.completed` |
| `failed-model-missing.jsonl` | `--model m2-probe-missing-model` (synthetic) | 1 | `run.terminal.failed`, reason "model … does not exist or you lack access" |
| `failed-api-key-rejected.stderr.txt` | `META_API_KEY` set to a synthetic bad key; no stream | 1 | none (stderr only) |
| `failed-missing-credentials.stderr.txt` | no `META_API_KEY`, no login (empty `XDG_CONFIG_HOME`); no stream | 1 | none (stderr only) |
| `refused-absolute-outside-workspace.jsonl` | `edit_file` by absolute path into a directory under `$HOME` | 0 | `run.terminal.completed` (`tool.result` failure: `tool failed: absolute path is outside the workspace`) |
| `refused-path-escapes-dotdot.jsonl` | `edit_file` by a `../` path to the same directory | 0 | `run.terminal.completed` (`tool.result` failure: `tool failed: path escapes workspace`) |
| `refused-path-escapes-symlink.jsonl` | `edit_file` through a symlink inside the workspace that points at the same directory | 0 | `run.terminal.completed` (`tool.result` failure: `tool failed: path escapes workspace`) |
| `policy-denied-shell.jsonl` | the `monitor` tool, which is shell execution | 0 | `run.terminal.completed` (`tool.result` failure: `tool failed: tool policy denied shell execution`) |
| `policy-denied-shell-component-relative.jsonl` | the `monitor` tool with a `../` path into the component directory, run in the rework with the existing login and `muse-ember-1.0`; one tool call | 0 | `run.terminal.completed` (`tool.result` text: `tool failed: tool policy denied shell execution`) |
| `refused-web-host.jsonl` | `web_fetch` (`mcp__expert_web__web_fetch`) to a host with no grant, through the real `_expert-web` server on a temporary journal | 0 | `run.terminal.completed` (`tool.result` text: `The Product Owner has been asked to allow probe.example.invalid.`) |

Exit code 0 with a tool failure means the run completed and the model reported the tool's refusal as
its reply. The refusal texts are what the tool returned; the file on disk was not written (checked
after each run). The `policy-denied-shell.jsonl` side-effect intent shows `allow:policy`; the denial
comes from the tool's own shell check, not from the policy decision recorded on the intent.

These fixtures are the evidence for M5's tests. A later `muse` that stops refusing the three texts
above, or that changes the `run.terminal` shape, should fail those tests.

The `_expert-web` tool reaches the model as `mcp__expert_web__web_fetch`. `--approval-mode never`
approves every tool call with no judge; it is not an allow-list like Claude's `--allowedTools`. Under
`untrusted` and `on-request`, the same `web_fetch` call was allowed by `allow:llm_judge`. The refusal
above came from the server, not from Muse's approval mode.

## Write matrix (Muse 1.4.15, Expert flags, one run per case)

Each case is one prompt asking the model to call one tool once on one path. Target a is the scratch
directory (the workspace), b a temporary directory outside it, c a directory under `$HOME` that
stands for a component. The forms are: `abs` (absolute path), `symlink` (a relative path through a
symlink inside the workspace), `symlink-abs` (an absolute path through that symlink), and `../` (a
relative path that climbs out of the workspace). "written" and "edited" were checked on disk: the file
exists with the new content. "refused" was checked on disk too: nothing was created or changed.

| Tool, target | abs | symlink | symlink-abs | `../` |
| --- | --- | --- | --- | --- |
| `write_file`, a | written | written | written | written |
| `write_file`, b | written | written | written | written |
| `write_file`, c | refused: `absolute path is outside the workspace` | refused: `path escapes workspace` | refused: `absolute path is outside the workspace` | refused: `path escapes workspace` |
| `edit_file`, a | edited | edited | edited | edited |
| `edit_file`, b | edited | edited | edited | edited |
| `edit_file`, c | refused: `absolute path is outside the workspace` | refused: `path escapes workspace` | refused: `absolute path is outside the workspace` | refused: `path escapes workspace` |
| `subagent`, a | child wrote the file | child wrote the file | child wrote the file | child wrote the file |
| `subagent`, b | child wrote the file | child wrote the file | child wrote the file | child wrote the file |
| `subagent`, c | child refused: `absolute path is outside the workspace` | child refused: `path escapes workspace` | child refused: `absolute path is outside the workspace` | child refused: `path escapes workspace` |
| `monitor`, a | policy denied: `tool policy denied shell execution` | same | same | same |
| `monitor`, b | policy denied: `tool policy denied shell execution` | same | same | same |
| `monitor`, c | policy denied: `tool policy denied shell execution` | same | same | policy denied: `tool policy denied shell execution` (called once in the rework; first attempt, in the first set, the model declined) |
| `workflow`, a | launched; child wrote the file | launched; child wrote the file | launched; child wrote the file | launched; child wrote the file |
| `workflow`, b | launched; child wrote the file | launched; child wrote the file | launched; child wrote the file | launched; child wrote the file |
| `workflow`, c | launched; child refused: `absolute path is outside the workspace` | launched; child refused: `path escapes workspace` | launched; child refused: `absolute path is outside the workspace` | launched; child refused: `escapes the filesystem root` |

Result: no route wrote a file under the component directory. For the component the only files were
the four existing `original` files, unchanged after every run. The `workflow` `../` refusal uses a
different text from the others; M5 must not assume one text for every tool. The `subagent` and
`workflow` child results are read from the child's reply and from the files on disk, because a child's
tool calls are not in the parent stream. The `monitor`, `c`, `../` case was not called in the first set,
because the model declined. In the rework the model called it once and the tool returned the policy
denial, so `monitor` wrote nothing under the component. That denial comes from the policy, before any path
check, so this case does not show how the path check treats `../` into a component for `monitor`; M5 must
not rely on a path check for `monitor`. The write results come from one run of this matrix plus one
rework run of the `monitor` case. A model can decline a call, so a case with no call is inconclusive, not
a success or a refusal.

## Session evidence (three-turn and resume runs)

Every turn of the three-turn run passed `--output-schema`, and each stream's session id equals the
`--session-id` passed. The redacted ids are shown below; both columns are the same placeholder.

| Run | Turn | Passed `--session-id` | Stream session id | Equal | Schema | Exit |
| --- | --- | --- | --- | --- | --- | --- |
| three-turn | 1, never-used UUID (`success-schema-turn1.jsonl`) | `…000002` | `…000002` | yes | yes | 0 |
| three-turn | 2, used UUID (`resume-turn2.jsonl`) | `…000002` | `…000002` | yes | yes | 0 |
| three-turn | 3, used UUID (`resume-turn3.jsonl`) | `…000002` | `…000002` | yes | yes | 0 |
| kill run | 2, killed by SIGTERM (`interrupted-sigterm.jsonl`) | `…000108` | `…000108` | yes in the stream; the passed value was not compared | yes | 143 |
| kill run | 3, resumed (`resumed-after-sigterm.jsonl`) | `…000108` | `…000108` | yes in the stream; the passed value was not compared | yes | 0 |
| single turn, `monitor` run (`policy-denied-shell-component-relative.jsonl`) | 1 | `…000901` | `…000901` | yes (raw ids compared before redaction) | yes | 0 |

For the kill run the stream id is the same before and after the kill, which the fixtures show; the
comparison against the passed value was not printed during the probe, so it is recorded here as not
shown. The three-turn comparison was printed during the probe.

## add_memory location

Three `add_memory` calls ran in the rework with the Expert's flags and `muse-ember-1.0`. The data
directory was a temp dir. Two calls used scope `personal_project` from two different sessions (two session
ids) in the same scratch directory; one used scope `project` in that directory. The scope was named in the
prompt, so the model's own scope choice was not tested here.

| Call | Scope | Where the note went | Tool result |
| --- | --- | --- | --- |
| A | `personal_project` | `$XDG_DATA_HOME/muse/memory/projects/<key>/MEMORY.md` | `success`, `memory note written` |
| B | `personal_project`, second session, same scratch directory | the same `MEMORY.md` as A; both notes are in it | `success`, `memory note written` |
| C | `project` | `<workspace>/.agents/memory/MEMORY.md` | `success`, `memory note written` |

`<key>` is derived from the workspace path. A stable scratch directory per conversation keeps
`personal_project` memory to one directory per conversation: the two sessions shared one key directory and
one `MEMORY.md`. `project` scope lives inside the workspace, so it follows that directory. The
`missing field path` failure seen once in the first set did not recur in these calls. These three streams
are not kept as fixtures; the tool results above are what they recorded.

## Real configuration written during the probes

The probes of this milestone were not confined to temporary directories. The real
`~/.config/muse/auth.json` (322 bytes) and its directory `~/.config/muse` had their modification times
changed during the first set of probes: `auth.json` from 1794503674 to 1794515004 (epoch seconds). Its
size did not change, and the probe never opened the file. Most of the runs in that set used the
default configuration directory (only the missing-credential run set `XDG_CONFIG_HOME`), so Muse
itself most likely rewrote the file, but the run is not identified. The settings and trust files and
the lock files under `~/.config/muse` did not change. This breaches the rule that nothing outside the
temporary directories and this directory is written. It is not restored: restoring the time would be
another write to the real file, and its contents were never read.

The rework ran one `monitor` call and three `add_memory` calls with the existing login, as the Operator
authorized. They used the default configuration directory, because the login is found only there (an
empty `XDG_CONFIG_HOME` gives the missing-credentials error). `XDG_DATA_HOME`, `XDG_STATE_HOME` and
`XDG_CACHE_HOME` were set to a temp dir, and the runs logged in with them. This work did not open the
login file, and it did not check the file's modification time after these runs, as the Operator asked. So
whether these runs rewrote `auth.json` is not known. The Operator accepted this login-file side effect as
the exception to the write restriction for these runs. The earlier mtime change is still a breach: it is
recorded here, it is not restored, and the restriction is not claimed satisfied.

For the data, state and cache directories, the isolation works with the login, as the rework's runs show.
The configuration directory cannot be isolated while the login is used, because the login file lives
there. An earlier check with a bogus `META_API_KEY` (no login used) showed the server rejecting the key,
Muse writing its lock and session files only under the temp dirs, and no change to the stat of the real
`~/.config/muse` and `~/.local/share/muse`.
