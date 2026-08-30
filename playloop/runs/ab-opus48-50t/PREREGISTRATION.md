# Pre-registration — constrained vs free-form, one model, one length

**Written and committed BEFORE the first measured run.** Commit this file first; the results
land in a later commit. If what follows and the results disagree, the results are what happened
and this file is not to be edited to match them.

## Why this exists

Two claims in this repo currently outrun their evidence:

1. No committed run uses a decoder-enforced Claude path. `--backend api` was checked turn by turn
   against a committed menu — a unit test, not a result.
2. The "16.6% → 0" headline is not a controlled comparison. 16.6% is Opus over 50 free-form turns
   with no Opus constrained run to pair with it; the nearest same-model pair is Sonnet at 30
   free-form turns against a 2-turn constrained smoke. It crosses both model and run length.

One experiment closes both.

## Design

| | |
|---|---|
| Model | `claude-opus-4-8`, both arms |
| Backend | `--backend api` (Anthropic Messages API), **both arms** |
| Turns | `--max-turns 50`, both arms |
| Seeds | `42` and `43` — **each seed is run in both arms** (paired) |
| Runs | 2 per arm, 4 total. Decided now; the count does not move. |
| Arm A | `--mode freeform` |
| Arm B | `--mode constrained` |

The **only** difference between arms is `--mode`. Both arms use the same backend on purpose: a
CLI arm against an API arm would confound the mode with the enforcement mechanism, which is the
exact conflation this experiment exists to remove.

Opus 4.8 because 16.6% is an Opus number and Opus is the only model with headroom here — Sonnet's
free-form rate is already 1.3%, so a constrained arm against it could show almost nothing.

Exact commands:

```
.venv/bin/python -m playloop.loop --backend api --model claude-opus-4-8 --max-turns 50 \
    --mode freeform    --seed 42 --out-dir playloop/runs/ab-opus48-50t/freeform-seed42
.venv/bin/python -m playloop.loop --backend api --model claude-opus-4-8 --max-turns 50 \
    --mode constrained --seed 42 --out-dir playloop/runs/ab-opus48-50t/constrained-seed42
```
…and the same pair with `--seed 43`.

## Primary metric

**Unusable-selection rate: of the per-actor decisions the model returned, the fraction naming
something the environment was not offering.** One concept, measured in each arm's own units,
because the arms do not have the same output vocabulary:

- **Free-form arm:** `(illegal + malformed) / (applied + illegal + malformed)`.
  This is `illegal_rate` as `loop.py` already computes it. `illegal` = the action string was not
  in that actor's legal set; `malformed` = the move object was unusable.
- **Constrained arm:** `(bad_index + unknown_actor) / (attempted + skipped + bad_index +
  unknown_actor)`.
  `bad_index` = an option number not on that actor's menu; `unknown_actor` = an actor key that is
  not on the board. `illegal` is **not** used here: `apply_constrained` returns it as a literal 0
  by construction, so reporting it as a measurement would be circular.

**These denominators are not identical and the comparison is therefore not exact.** Free-form
counts decisions the model volunteered; constrained counts decisions across every actor, because
the schema requires an answer for each. Stated here so it cannot be presented later as cleaner
than it is.

## Secondary metrics — reported either way, favourable or not

`retries`, `stale`, `applied`, actors acted on per turn, turns completed, `run_error`, final
score, cities founded, techs, `total_input_tokens` / `total_output_tokens`, wall-clock.

Game outcome is reported but is **not** the claim: this experiment is about output legality, not
Freeciv skill.

## What would mean the constraint did NOT help — written before the numbers exist

Any one of these is a negative result, and gets committed and written into the README as such:

1. **The free-form arm does not leak.** If Arm A's unusable-selection rate is **< 2.0%** on both
   seeds, then Opus on this representation at this length does not have the problem the headline
   describes, the 16.6% does not reproduce, and the constraint is solving something that was
   already rare.
2. **Enforcement leaks.** If Arm B shows **any** non-zero `bad_index` or `unknown_actor`, then
   "unrepresentable" is false as implemented and the decoder guarantee does not hold end to end.
3. **Constraint costs play.** If Arm B's final score is materially below Arm A's on both seeds,
   the constraint buys legality at the price of playing worse — reported as a cost, not buried.
4. **It does not finish.** If either arm cannot complete 50 turns without `run_error`, the
   comparison is void at that length and will be reported as void rather than truncated to
   whatever length happens to work.

An arm being merely *equal* is also a negative result. "No measurable difference" gets reported.

## Stopping rule and re-runs

Exactly 4 measured runs. If a run dies on an environment error (not a model error), it is re-run
**at most once** on the same seed, and **both** the failed and the re-run are committed, with the
failure named in the report. No run is discarded for having an unwelcome number.

## Pilot, declared in advance

One 2-turn plumbing check on **seed 99** (a seed used by no measured run) may be run first, only
to confirm the env, the API backend and the writer work end to end. It is not analysed, not
committed as a result, and its numbers do not enter the report except as "the pilot ran".

## Cost

`--backend api` reports `total_cost_usd: 0`; it records `total_input_tokens` /
`total_output_tokens` instead. Cost is computed after the fact from those counts at Opus 4.8's
published rate ($5 / $1M input, $25 / $1M output) and reported. Estimate approved in advance:
**≈ $8 for all four runs.**

## Instrument change made before running, disclosed

Token counts were not being persisted anywhere — `ChoiceResult.raw` was never written to the
transcript — so the cost metric above was unmeasurable as the code stood. `input_tokens` /
`output_tokens` now flow from the API backend into `metrics.jsonl` and `summary.json`. No
decision logic was touched.
