# Reasoner spike (D1)

Run on 2026-10-31 with Claude Code 2.1.316 and codex-cli 0.166.0, each asked one `assess_blocker`-shaped
question with a JSON schema, from an empty scratch directory. Both returned
`{"verdict":"host_tool","tool":"ffmpeg"}` (Claude on `haiku`, about 0.001 USD).

## Claude

```
claude -p --model <model> --output-format json --json-schema '<schema>' \
  --tools "WebSearch,WebFetch,Read,Bash(command -v:*),Bash(brew info:*),Bash(brew search:*)" \
  --permission-mode dontAsk --setting-sources "" --no-session-persistence "<prompt>"
```

* The answer is the `structured_output` object of the one JSON result on stdout (`result` holds it as
  text). `is_error` and `subtype` say whether the call worked.
* `--tools` limits the tools to the list, with command patterns for Bash. `dontAsk` refuses what is not
  listed instead of waiting for a person. `--setting-sources ""` loads no settings, hooks or CLAUDE.md.
* Usage is `total_cost_usd`, `usage` and `duration_api_ms` in the same object.

## Codex

```
codex exec --ephemeral --skip-git-repo-check -s read-only -C <scratch> --output-schema <file> "<prompt>"
```

* The schema must be a file. The answer is the last line of stdout (`--output-last-message <file>` also
  exists). Token use is printed after it.
* Codex cannot limit its shell to named commands. `-s read-only` stops writes only; web search is off
  unless `--search` is given, and the adapter does not give it. The adapter therefore runs with no
  lookups at all (design §8: an adapter whose CLI cannot restrict tools runs with none).
* Residual risk: with `-s read-only` the model can still read files on the host. Accepted, because it
  can write nothing and the answer is validated by code.
