# Reasoner spike (D1)

Run on 2026-10-31 with Claude Code 2.1.316 and codex-cli 0.166.0, each asked one `assess_blocker`-shaped
question with a JSON schema, from an empty scratch directory. Both returned
`{"verdict":"host_tool","tool":"ffmpeg"}` (Claude on `haiku`, about 0.001 USD).

## Claude

```
claude -p --model <model> --output-format json --json-schema '<schema>' \
  --tools "WebSearch,WebFetch,Bash(command -v:*),Bash(brew info:*),Bash(brew search:*)" \
  --permission-mode dontAsk --setting-sources "" --no-session-persistence [--effort <level>] "<prompt>"
```

* The answer is the `structured_output` object of the one JSON result on stdout (`result` holds it as
  text). `is_error` and `subtype` say whether the call worked.
* `--tools` limits the tools to the list, with command patterns for Bash. `dontAsk` refuses what is not
  listed instead of waiting for a person. `--setting-sources ""` loads no settings, hooks or CLAUDE.md.
* `Read` is not in the list (D3). It cannot be limited to the scratch directory, and a model that can
  read any host file is an exfiltration path for injected escalation text.
* `--effort <level>` takes `low`, `medium`, `high`, `xhigh` or `max` (`claude --help`, 2.1.316). A rung's
  effort is passed as this flag, and a rung without one passes no flag.
* Usage is `total_cost_usd`, `usage` and `duration_api_ms` in the same object. They go into the signal's
  verdict record as the call record (design §8, migration 14).

## Codex

```
codex exec --ephemeral --skip-git-repo-check -s read-only -C <scratch> --output-schema <file> "<prompt>"
```

* The schema must be a file. The adapter reads the answer from the `--output-last-message <file>`.
* Usage: in the spike's merged output (`2>&1`), a `tokens used` line and its count (`15,607`) came
  before the final answer line. The adapter reads the last such count from stderr or stdout (the
  spike did not separate the streams) and records it as `tokens_used`. Codex prints no duration or
  cost, so those fields stay unset.
* Effort: `codex exec --help` (codex-cli 0.166.0) has no effort flag; its only match for "effort",
  "reasoning", "usage" or "token" is the word "Usage:". A Codex rung's effort is therefore not passed
  and is ignored. A Claude rung's effort goes to `--effort`.
* Codex cannot limit its shell to named commands. `-s read-only` stops writes only; web search is off
  unless `--search` is given, and the adapter does not give it. The adapter therefore runs with no
  lookups at all (design §8: an adapter whose CLI cannot restrict tools runs with none).
* Residual risk: with `-s read-only` the model can still read files on the host. Accepted, because it
  can write nothing and the answer is validated by code.
