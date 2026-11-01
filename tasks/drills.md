# Drills of design §15: coverage map

Every drill of [design §15](../docs/design.md#15-proving-it-works) is listed once (D1–D22), and every
measure of its table once (M1–M5). Test paths are `tests/unit/<file>::<name>`. `NEW` marks a test
added in E2 to `tests/unit/test_composition.py`: a composed run with the fake Shikiru session, the
fake Bot API and the test-time `NapClock`. Drills marked `H#` cannot run offline; their exact steps
are in [Human drills](#human-drills) and the gate never runs them.

## Drills

### Missing tool

| # | Drill | Covered by | Status |
|---|---|---|---|
| D1 | Approve, install, check, task continues | `tests/unit/test_composition.py::test_host_tool_approval_survives_restart_open_and_reply`; `tests/unit/test_install_executor.py::test_a_passing_check_after_a_clean_exit_answers_and_waits_for_its_event`; `tests/unit/test_replies.py::test_install_tap_approves_a_stored_op_once` | covered (real host: H1) |
| D2 | The same with Decline | `NEW test_a_declined_install_runs_no_command_and_leaves_the_escalation_to_the_po`; `tests/unit/test_replies.py::test_decline_is_durable_and_product_forward_is_replay_safe` | covered (real host: H1) |
| D3 | The same with a tool that is already there | `tests/unit/test_install_preparation.py::test_existing_tool_is_forwarded_with_its_path`; `tests/unit/test_tool_check.py::test_a_found_tool_gives_the_path_the_shell_printed` | covered |
| D4 | The same with a hostile package name | `tests/unit/test_tool_check.py::test_a_hostile_name_is_refused_before_the_shell_starts`; `tests/unit/test_recipes.py::test_a_hostile_or_unknown_name_is_refused_with_its_reason` | covered (real host: H1) |
| D5 | The same with a failing install | `tests/unit/test_install_executor.py::test_a_clean_exit_with_a_failing_check_forwards_and_fails`; `tests/unit/test_install_executor.py::test_a_non_zero_exit_with_a_missing_tool_forwards_the_output_tail_and_fails`; `tests/unit/test_install_runs.py::test_a_failed_check_is_recorded_once_for_an_issued_install` | covered (real host: H1) |

### Transient agent error

| # | Drill | Covered by | Status |
|---|---|---|---|
| D6 | One retry, then a forward when it repeats | `tests/unit/test_infrastructure.py::test_an_agent_error_resolves_and_the_po_is_not_told`; `tests/unit/test_infrastructure.py::test_a_second_retry_of_the_same_task_and_reason_forwards_naming_the_first`; `tests/unit/test_composition.py::test_a_retry_is_given_back_to_shikiru_once_and_the_po_gets_no_message` | covered |

### Quota exhaustion

| # | Drill | Covered by | Status |
|---|---|---|---|
| D7 | The wait, the message, the retry at the reset | `tests/unit/test_quota_wait.py::test_a_reset_in_three_hours_parks_until_it_plus_a_minute_with_one_notice`; `tests/unit/test_quota_wait.py::test_the_call_after_the_wake_time_retries_once_and_tells_nothing_more`; `tests/unit/test_composition.py::test_a_quota_wait_tells_the_po_then_retries_once_after_the_reset` | covered (real agent: H6) |
| D8 | A reset more than 24 hours away | `tests/unit/test_quota_wait.py::test_a_quota_escalation_forwards_when_no_wait_fits` (case `reset-beyond-the-limit`) | covered |

### Kills and restarts

| # | Drill | Covered by | Status |
|---|---|---|---|
| D9 | Kill an engine | `tests/unit/test_liveness.py::test_engine_that_died_after_start_is_down`; `tests/unit/test_engine_restart.py::test_a_down_engine_is_started_once_confirmed_and_announced_once`; `tests/unit/test_composition.py::test_an_engine_down_forwarded_for_want_of_a_restart_closes_once_the_engine_is_up` | covered (real process: H4) |
| D10 | Kill the Operator mid-install | `tests/unit/test_install_reconcile.py::test_a_crash_after_claim_before_command_reasks_without_running`; `tests/unit/test_install_reconcile.py::test_a_crash_after_run_before_answer_is_retried_by_the_restart_check`; `tests/unit/test_install_reconcile.py::test_a_nonzero_run_interrupted_during_forward_is_checked_and_answered_at_restart` | covered at each crash point of the journal; real kill: H2 |
| D11 | Kill the Operator mid-resolve | `NEW test_an_operator_killed_mid_resolve_confirms_the_answer_shikiru_recorded`; `tests/unit/test_replies.py::test_claimed_failure_is_left_for_reconciliation` | covered (real kill: H2) |
| D12 | Kill the Operator mid-send | `NEW test_a_question_killed_mid_send_is_sent_once_more_and_then_confirmed`; `tests/unit/test_outbox.py::test_a_crash_between_sending_and_confirming_resends_once_with_the_signal_reference`; `tests/unit/test_reconciler.py::test_crash_mid_send_with_the_escalation_open_shows_the_question_again` | covered (real kill: H2) |
| D13 | Reboot the host: engines come back | `tests/unit/test_liveness.py::test_clean_stop_before_this_run_is_down_at_once`; `tests/unit/test_service.py::test_the_launchd_plist_runs_the_command_with_the_login_path`; `tests/unit/test_service.py::test_the_systemd_user_unit_runs_the_command_with_the_login_path` | rule and unit level; real reboot: H3 |
| D14 | An engine the PO stopped stays stopped across an Operator restart | `NEW test_an_engine_the_po_stopped_stays_stopped_across_an_operator_restart`; `tests/unit/test_liveness.py::test_respected_stop_stays_respected_after_an_operator_restart` | covered (real host: H3) |

### Pull-request mode

| # | Drill | Covered by | Status |
|---|---|---|---|
| D15 | The review message | `tests/unit/test_review.py::test_a_pull_request_gets_one_review_message_with_its_link_and_signal_number`; `tests/unit/test_composition.py::test_a_pull_request_opened_is_sent_for_review_and_the_task_done_closes_its_message` (sent message) | covered |
| D16 | The merge | `tests/unit/test_composition.py::test_a_pull_request_opened_is_sent_for_review_and_the_task_done_closes_its_message` (edit to the merged line); `tests/unit/test_event_source.py::test_resolved_escalation_and_pr_lifecycle_close_the_matching_signals`; `tests/unit/test_review_closing.py::test_each_way_a_pull_request_closes_edits_every_copy_without_buttons` | covered |
| D17 | The notice | `tests/unit/test_notice.py::test_the_last_task_of_a_family_announces_it_once` | rule level; the composed PR test does not assert it |
| D18 | A pull request closed unmerged | `NEW test_a_pull_request_closed_unmerged_closes_its_review_message`; `tests/unit/test_event_source.py::test_a_pr_closed_escalation_closes_its_review_and_still_raises_its_own_signal` | covered |

### Answers

| # | Drill | Covered by | Status |
|---|---|---|---|
| D19 | Two answers to the same question from two chats | `NEW test_two_answers_to_one_question_from_two_chats_resolve_once`; `tests/unit/test_open.py::test_two_confirm_copies_of_one_approval_issue_one_resolve`; `tests/unit/test_replies.py::test_answer_duplicate_after_reconstruction_issues_once` | covered |
| D20 | An answer given in tmux | `NEW test_an_answer_given_in_tmux_closes_the_question_in_telegram`; `tests/unit/test_replies.py::test_resolved_elsewhere_invalidates_even_before_the_event_is_journaled`; `tests/unit/test_open.py::test_an_escalation_resolved_elsewhere_is_not_shown_open` | covered |

### Reasoner

| # | Drill | Covered by | Status |
|---|---|---|---|
| D21 | The Reasoner unavailable: everything forwarded with a note, one health notice, nothing lost | `tests/unit/test_assessor.py::test_when_every_rung_fails_the_decision_goes_to_product_with_the_reasoner_note`; `tests/unit/test_assessor.py::test_repeated_failures_keep_one_reasoner_signal_open`; `tests/unit/test_assessor.py::test_without_a_reasoner_a_decision_goes_to_product_and_the_ladder_is_not_asked`; `tests/unit/test_notice.py::test_a_health_message_is_sent_once_and_its_closing_edits_it_to_recovered` | unit level; the Reasoner's health message in Telegram is not composed |
| D22 | A ladder whose first rung fails | `tests/unit/test_reasoner_ladder.py::test_a_failed_rung_is_skipped_for_the_cooldown_and_tried_again_after_it`; `tests/unit/test_assessor.py::test_a_deadline_interrupted_rung_cools_down_and_recovers_after_the_cooldown` | covered |

## Measures

Always on; all of them are read from journal timestamps and rows by `shikiri-yaku stats <product>`
(`src/shikiri_yaku/app/stats.py`). Figures are computed over the instance's own journal only.

| # | Measure (§15) | Source | Tested by | Status |
|---|---|---|---|---|
| M1 | PO wait time: raised to resolved, and the share spent waiting for the PO | journal | `tests/unit/test_stats.py::test_waits_start_at_the_first_queued_question_and_copies_add_nothing`; `tests/unit/test_stats.py::test_the_share_is_summed_waiting_over_summed_completed_lifetime`; `tests/unit/test_stats.py::test_no_completed_escalation_leaves_the_share_unavailable` | covered |
| M2 | Handled without the PO, per category and action | journal | `tests/unit/test_stats.py::test_handled_without_the_po_counts_resolved_signals_with_no_question_or_approval` | **partial**: counted per category only; no action breakdown exists in `stats.py` (see gaps) |
| M3 | Escalation mix: count per reason and per Reasoner verdict | journal | `tests/unit/test_stats.py::test_the_escalation_mix_counts_reasons_and_names_an_absent_verdict` | covered |
| M4 | Misroutes: verdicts reviewed by hand, first month, every `decision_required` | review | not printed by `stats` by design; human drill H7 | human review |
| M5 | Installs: approved, declined, checks failed | journal | `tests/unit/test_stats.py::test_install_outcomes_count_recorded_approvals_declines_and_failed_checks`; `tests/unit/test_stats.py::test_installs_are_counted_by_current_state_and_other_kinds_are_left_out` | covered |

## Gaps this map records

* **M2 per action.** Design §15 asks for handled-without-the-PO per category and per action; `stats`
  prints per category only. Not changed in E2 (it is a feature, not a drill). Needs a decision.
* **D17 in the composed PR test.** The composed PR test asserts the review message and the merge
  edit; the family notice after `task_done` is asserted at rule level only.
* **D10 mid-install in a composed run.** Covered at each journal crash point, not by a composed run.
  The real kill is H2.
* **D21 health message in Telegram.** The Reasoner's health signal is tested at unit level; the
  message that reaches Telegram is only composed for the Operator's own health message.

## New composed tests and their deliberate breaks

Each test was run against the break below; it failed for the stated reason. Every break was
reverted afterwards (`git status` shows only the test file changed).

| Test (`tests/unit/test_composition.py::`) | Drill | Break | Failure seen |
|---|---|---|---|
| `test_two_answers_to_one_question_from_two_chats_resolve_once` | D19 | `src/shikiri_yaku/app/replies.py` `_open_escalation`: the check for a resolved escalation replaced with `if False:` | TimeoutError waiting for "Already resolved" |
| `test_an_answer_given_in_tmux_closes_the_question_in_telegram` | D20 | `src/shikiri_yaku/app/event_source.py` `ESCALATION_RESOLVED` branch: the `Closing` change removed, the `Resolved` kept | AssertionError: signal state FORWARDED, not CLOSED |
| `test_a_pull_request_closed_unmerged_closes_its_review_message` | D18 | `src/shikiri_yaku/app/event_source.py` `_escalation`: the review closing for `pr_closed` replaced with `pass` | TimeoutError waiting for the edit |
| `test_a_question_killed_mid_send_is_sent_once_more_and_then_confirmed` | D12 | `src/shikiri_yaku/app/outbox_sender.py`: the `confirm` call replaced with `pass` | TimeoutError waiting for the confirmation |
| `test_an_operator_killed_mid_resolve_confirms_the_answer_shikiru_recorded` | D11 | `src/shikiri_yaku/app/reconciler.py` `_decide`: confirm replaced with fail | TimeoutError waiting for the confirmation |
| `test_an_engine_the_po_stopped_stays_stopped_across_an_operator_restart` | D14 | `src/shikiri_yaku/app/liveness_source.py` `_clean_stop`: the `respect_stop` call replaced with `pass` | TimeoutError: the respected stop is never journaled |
| `test_a_declined_install_runs_no_command_and_leaves_the_escalation_to_the_po` | D2 | `src/shikiri_yaku/extensions/infrastructure/install_replies.py` `_decide` line 103: `if verb == _INSTALL:` widened to `if verb in (_INSTALL, _DECLINE):` | TimeoutError: the declined note is never sent |

## Human drills

These need a real host, a real package manager, a real agent CLI or a real reboot. Use a throwaway
host. Use the Telegram test bot and the test chat. Keep `config.toml` and the token file in the
instance directory `<home>/<product>` (default home `~/.shikiri-yaku`). Never paste the token or a
chat id into a report. Commands below are the CLI as built (`shikiri-yaku --help`).

### H1. Missing tool on a throwaway host (D1–D5)

Setup: a tool that is not installed on the host (name it `<tool>`), and a Shikiru engine with its
fake agent that asks for it.

1. `shikiri-yaku doctor <product>`. Expect every check to pass.
2. In terminal A: `shikiri-yaku run <product>`. Leave it running.
3. Start the fake agent's task that needs `<tool>`. Expect a message headed "S<n> · Install approval"
   with the buttons **Install** and **Decline**.
4. Tap **Install**. Expect the tool to be found by the check, and the task to continue in Shikiru.
   Then run `shikiri-yaku stats <product>`: the install outcomes show `approved: 1`.
5. Start a second task that asks for a second missing tool. Tap **Decline**. Expect a question to the
   PO with a note that it was declined, and `<tool2>` still missing on the host. `stats` shows
   `declined: 1`.
6. Start a task that asks for a tool named `x;touch /tmp/shikiri-h1`. Expect no **Install** button,
   a message that gives the refusal, and no file at `/tmp/shikiri-h1`.
7. Start a task that asks for a package the package manager does not know. Tap **Install**. Expect
   the failure forwarded with the output tail. Check the op with
   `sqlite3 -readonly <home>/<product>/journal.db "SELECT kind, state FROM ops;"`.

### H2. Operator killed mid-install, mid-resolve and mid-send (D10–D12)

Setup: as H1. Use an install that runs for more than 10 seconds (a large package).

1. Terminal A: `shikiri-yaku run <product>`.
2. Mid-install: tap **Install**. While the install runs, in terminal B:
   `kill -9 $(pgrep -f "shikiri-yaku run <product>")`.
3. In terminal A: `shikiri-yaku run <product>`. Expect the install not to run twice: the ops table
   (`sqlite3 -readonly ... "SELECT kind, state FROM ops;"`) shows one install op that ends in one of
   its final states, and the PO sees at most one new question.
4. Mid-resolve: answer a question. Within one second after the answer, `kill -9` the Operator as in
   step 2. Restart as in step 3. Expect the answer to reach Shikiru once (a `resolve` op ends
   `confirmed`) and the question not to come back.
5. Mid-send: start a task that raises a question. Kill the Operator while the question is being sent;
   repeat until the kill lands in the send. Restart. Expect at most one extra copy of the question,
   and in `messages` (`SELECT signal_number, purpose, delivery FROM messages;`) its delivery ends in a
   confirmed state.

### H3. Host reboot: engines come back, a stopped engine stays stopped (D13, D14)

1. For each product: `shikiri-yaku install-service <product>`. It prints `wrote <path>` and
   `start it with: <command>`. Run that command.
2. Stop one engine the way the PO does: find its process with `pgrep -fl "shikiru --project"`, then
   `kill -TERM <pid>`. Wait 5 minutes, which is longer than `stop_grace_min` (2) and the one-minute
   liveness interval.
3. Reboot the host. After login, `shikiri-yaku doctor <product>` for each product.
4. Expect the engines that were running to be up. Expect the engine stopped in step 2 to stay down,
   with no restart message. `shikiri-yaku stats <product>` shows the same.

### H4. Real engine crash and restart (D9)

1. Terminal A: `shikiri-yaku run <product>`.
2. Find the engine process with `pgrep -fl "shikiru --project"`, then `kill -9 <pid>` (a crash, so
   no stop event).
3. Expect within about one minute one "engine down" message, the Operator starts the engine, and
   the engine is up within 60 seconds. Expect no second message.
4. Repeat step 2 twice within an hour. Expect the second restart to be refused with the failed
   checks, as the restart budget says.

### H5. Two real projects (all drills, D1–D22 as applicable)

1. Prepare two small projects, `<product-a>` and `<product-b>`, each with its own instance directory.
2. `shikiri-yaku doctor <product-a>` and `shikiri-yaku doctor <product-b>`. Expect both to pass.
3. Terminal A: `shikiri-yaku run <product-a>`. Terminal B: `shikiri-yaku run <product-b>`.
4. Run H1 steps 3 and 4 in each project. Expect each question in its own chat, and no answer in one
   project to appear in the other.
5. `shikiri-yaku stats <product-a>` and `shikiri-yaku stats <product-b>`. Each prints its own figures.

### H6. Real agent quota (D7, D8)

1. `shikiri-yaku run <product>`.
2. Drive a task until the real agent's quota window is at 100 percent (`/status` in Telegram shows
   the block).
3. Expect one message with the wait and the reset time. Expect no retry before the reset.
4. Wait until the reset plus one minute. Expect one retry in Shikiru and no further message if the
   task then proceeds.
5. If a window resets more than 24 hours away, expect the escalation forwarded with no wait. The
   offline test is `tests/unit/test_quota_wait.py::test_a_quota_escalation_forwards_when_no_wait_fits`.

### H7. Misroute review, first month (M4)

1. Once a week for the first month, run
   `sqlite3 -readonly <home>/<product>/journal.db "SELECT number, escalation_id, category, verdict, state FROM signals WHERE reason = 'decision_required' ORDER BY number;"`.
2. For each row, look up the escalation by `escalation_id` in Shikiru, and note what the PO and the
   agents did.
3. Mark each verdict right or wrong in a note outside this repository.
4. Count the wrong verdicts for each week. Decide from the count whether classification from text is
   good enough (design §15, misroutes).
