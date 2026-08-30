# Second attempt — abandoned when the API credit ran out

This is attempt 2 at `constrained-seed43`, made after attempt 1 was killed externally at turn 46
(see `../constrained-seed43-interrupted/`).

It ran cleanly for **25 turns**. From **turn 26 to turn 50** every model call returned:

    400 invalid_request_error — "Your credit balance is too low to access the Anthropic API."

The loop is built to survive a contestant failing, so it kept ending turns with nothing applied.
`summary.json` therefore says `turns_played: 50` and `completed_without_crash: true`, and **both
are misleading for this run**: 25 of those turns had no model in the loop at all. Read
`metrics.jsonl` and filter on `subagent_error`.

Over the 25 turns that did have a live model:

    bad_index 0 · unknown_actor 0 · stale 0 · retries 0 · 364 decisions
    pre-registered primary metric: 0/364 = 0.0000%

Committed rather than deleted, per the pre-registration: no run in this experiment was discarded.
It is **not** a result — it is the record of a failed attempt, and of a way this harness can
report a run as successful when it was not.
