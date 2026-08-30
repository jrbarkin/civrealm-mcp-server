# Interrupted run — kept deliberately

This is the first attempt at `constrained-seed43`. It reached **turn 46 of 50** and was killed
when the shell task driving it was stopped externally — not an environment error, not a model
error, and nothing to do with the run itself. It never wrote `summary.json` or `transcript.json`,
which are written at the end; `metrics.jsonl` survives because it is flushed every turn.

It is committed rather than deleted so the re-run is visible. Over the 46 turns it did reach:

    bad_index 0 · unknown_actor 0 · stale 0 · retries 0 · 848 decisions
    pre-registered primary metric: 0/848 = 0.0000%

The completed replacement is in `../constrained-seed43/`. Both are reported. No run in this
experiment was discarded for producing an unwelcome number.
