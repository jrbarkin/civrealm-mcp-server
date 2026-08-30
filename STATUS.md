# STATUS

Two pieces that share one env-access layer:

1. **`src/civrealm_mcp/`** — a minimal MCP server exposing **one** CivRealm (Freeciv) Gymnasium
   env as seven stdio tools, so an external MCP client can drive a game by hand. No LLM inside.
2. **`playloop/`** — a headless self-play harness (shape state → offer a constrained menu → ask a
   contestant → apply the legal choices → end turn), plus a two-side orchestrator and a win rule.

**Boundary worth remembering:** `playloop/` does **not** go over MCP. It imports
`civrealm_mcp.core` and drives the env in-process; nothing under `playloop/` imports `mcp`.
`core.CivRealmGame` is the single env-access layer — the MCP server is a thin async wrapper over
it, the play loop is a synchronous driver on top of it. The MCP server exists so an *external*
agent can play by hand; the play loop exists to run many turns headlessly with no JSON-RPC in the
way. `playloop/` is also where most of the Python lives; the server itself is 245 + 181 lines.

Last full verification (Docker + live games): **2026-06-15**, macOS on Apple Silicon (arm64),
against the Dockerized freeciv-web server. The offline test suites were re-run **2026-08-29** and
are still green (`pytest`, 15 tests, no Docker/model/network — CI runs the same on 3.10 + 3.11).

**Which runs a cloner can actually open:** `playloop/runs/` is gitignored apart from eight
committed directories — `main`, `sonnet-baseline`, `sonnet-fixed`, `constrained-smoke2`,
`local-llama1b-50turn`, `local-llama1b-schema`, `orch-test1`, `orch-test2`. Run names appearing
below that are *not* in that list (`constrained-smoke`, `local-smoke`, `probe-qwen-smoke`,
`probe-qwen-smoke2`, `constrained-haiku50`, `constrained-sonnet50`) exist only on the machine that
produced them; they are cited here as history, not as evidence you can check.

---

## The result worth keeping

**Per-turn JSON-Schema-constrained selection takes the illegal-action rate from 16.6% to 0**, and
holds it at 0 for every contestant tried — Sonnet, a local 1B, a local 4B, and a seeded random
picker. Free-form prompting leaks illegal proposals at a rate that is *model-specific* (Opus 4.8
16.6%, Sonnet 4.6 1.3% on the identical representation); constraining selection to a numbered menu
whose enum is built from `info['available_actions']` removes the failure mode entirely.

Said precisely, because the strength of the guarantee is backend-specific (README has the table):

- **On every backend**, the contestant returns option *numbers* that the loop resolves through the
  menu index, so an action the env never offered cannot be executed. `illegal: 0` is structural.
- **On `--backend local` (Ollama) and `--backend api` (Messages API)**, the schema additionally
  constrains the decoder, so an out-of-enum option cannot be *emitted*: unrepresentable rather
  than merely unlikely, in the strict sense.
- **On the default `--backend claude`**, the `claude` CLI validates the answer against the same
  schema and retries. There, an illegal choice is *rejected*, not unrepresentable — and every
  committed Claude run predates even that, having discarded the schema entirely.

The first bullet is what all the committed runs demonstrate, and it is the defensible claim.

## Current state

| component | state | evidence |
|---|---|---|
| MCP server, 7 tools | works | `scripts/acceptance_check.py` drives one turn through all 7 tools over stdio |
| Shared env layer (`core.CivRealmGame`) | works | both drivers run through it; acceptance test re-run after the refactor |
| State representation | works | `REPRESENTATION.md`, `EXAMPLE_SHAPED_STATE.txt` |
| Free-form mode (`--mode freeform`) | works; illegal rate is model-dependent | `runs/main`, `runs/sonnet-*` |
| Constrained mode (`--mode constrained`, default) | works; **no illegal action can be applied** | `runs/constrained-smoke2`, `runs/local-llama1b-*` |
| Retry feedback on invalid selections | works | `test_retry`: 6 tests / 28 assertions |
| Score-based win rule | works | `test_winrule`: 3 tests / 9 assertions; `final_result` read at cap in every constrained run |
| Two-side orchestrator | works — **with random contestants only** | `runs/orch-test1`, `runs/orch-test2` |
| Local backend (Ollama) | works: `llama3.2:1b`, `qwen3:4b` | `runs/local-llama1b-*` (committed); `runs/probe-qwen-smoke2` (local only) |
| **Two-LLM head-to-head** | **never run** | — |

The last row is the one that matters when reading older notes: the orchestrator has been verified
end-to-end, but only `random:1` vs `random:2` (`RandomContestant`, seeded, model-free). No game has
ever been played between two models.

## Verified — the MCP server

- **Environment.** Python 3.11 venv (uv) with `civrealm==0.1.2` and its pins (`gymnasium 0.29.1`,
  `selenium 4.9.1`, `tornado 6.3.2`, `ray 2.6.3`, `numpy 2.4.6`), plus `setuptools<81` because
  ray 2.6.3 imports the removed `pkg_resources`. Docker `civrealm/freeciv-web:latest` (amd64)
  runs under emulation on arm64; `curl http://localhost:8080/` → HTTP 200.
- **Upstream smoke tests.** `test_civrealm --max_turns=2` runs a single-player game to
  `Truncated: True`. The two-player variant (`--minp=2` + a second client on the same
  `client_port=6001`) is provably **one** game: each client's log shows the other player
  connecting, both share `mapseed 681897 / gameseed 950276`, and both print an identical final
  4-player `game results` dict.
- **Schema.** `SCHEMA.md` documents the observation dict (11 top-level keys), the `info` dict
  (`turn`, `available_actions`, optional `llm_info`), the action 3-tuple
  `(ctrl_type, actor_id, action_name)` with `None` = end-turn, and reward/terminated/truncated/
  winner signalling. Derived from source, a real unpickled observation, and live runs.
- **Server.** `src/civrealm_mcp/server.py` (official `mcp` SDK / FastMCP, stdio) owns one
  `FreecivBaseEnv` and exposes `start_game`, `get_observation`, `get_legal_actions`,
  `take_action`, `end_turn`, `get_status`, `close_game`. Model-agnostic; JSON in and out.
- **Acceptance check.** `scripts/acceptance_check.py` spawns the server over stdio as a real MCP
  client and runs start → observe → legal actions → take action → end turn → status → close, with
  no GUI and no direct civrealm calls. Passes: e.g. `take_action(unit, 104, build_city)` then
  `end_turn` advanced turn 1→2 with `last_reward: 1`, and `Reset with port: 6001` went to
  **stderr**, proving stdout stays clean for JSON-RPC.

### Bugs found and fixed during bring-up

Each was reproduced by a failing run, fixed, then re-run green.

1. **`pkg_resources` missing** (ray 2.6.3 on setuptools ≥81) → pin `setuptools<81`.
2. **Apple-Silicon port conflict** — the default port map publishes `7000-7009`, but macOS
   Control Center (AirPlay Receiver) owns 7000 → publish only `8080:80`, the one host port the
   client dials. The acceptance test connecting is the regression check.
3. **`This event loop is already running`** — CivRealm drives a tornado IOLoop inside
   `reset()/step()`, colliding with FastMCP's asyncio loop → offload every env call to one
   dedicated worker thread. The acceptance test failed on this and passes after the fix.
4. **`BeginTurnTimeout` in the server** — don't override `multiplayer_game`; raise
   `begin_turn_timeout` to 120s to absorb emulation latency.

## Verified — the play loop

**Representation** (`playloop/representation.py`) condenses the raw observation into a strategic
summary, the player's cities and units, and per-actor **exact legal actions**, with cryptic
`goto_*` keys annotated by compass direction. The full raw snapshot for turn 1 is kept in each
transcript so raw→shaped stays debuggable.

**Contestant** (`playloop/contestant.py`) is one pluggable slot with four implementations:
`ClaudeSubagentContestant` (spawns an isolated `claude -p ... --output-format json` per turn, tools
disabled — no paid API key needed; the schema goes to `--json-schema`, which the CLI *validates and
retries* rather than constraining the decoder), `AnthropicSDKContestant` (Messages API,
`output_config.format`, genuinely decoder-constrained — needs `ANTHROPIC_API_KEY`, and has no
committed run), `OllamaContestant` (local GGUF model, schema-constrained decoding), and
`RandomContestant` (seeded, model-free, for testing the harness).

**Constrained selection** is the default. `build_choice_menu()/render_menu()` emit a per-actor
numbered menu keyed `unit:104` / `city:110` / `tech:cur_player` / `gov:0`, option `0` = skip;
`menu_to_schema()` turns that menu into a per-turn JSON Schema — one integer property per actor,
each an `enum` of that actor's valid option numbers, all actors `required`,
`additionalProperties:false`. Where that schema is *enforced* differs by backend — Ollama's
`format` and the Messages API's `output_config.format` constrain the decoder; the `claude` CLI's
`--json-schema` validates and retries (see README's backend table). Independent of backend, the
contestant returns option *numbers*, so action strings never originate from the model, and
`apply_constrained` resolves numbers through the menu index and returns `illegal: 0` structurally.

One honest wrinkle: a choice can go **stale** *within* a turn — legal when the menu was built, no
longer legal after an earlier same-turn action moved a stacked unit. The first constrained smoke
(`runs/constrained-smoke`) tagged 2 such cases `illegal_stale` (a `build_road` and an `irrigation`
on stacked units) and still counted them in the illegal tally, hence its 0.222. They now get their
own `stale` counter and are skipped, which is what they always were: nothing illegal reached the
board in that run either.

**Retry feedback.** An invalid selection (bad option number, unknown actor key) returns an error
that enumerates the acceptable options for those actors and retries, merging only the corrected
actors, up to `max_retries`. `invalid_selections()` and `build_feedback()` are pure;
`resolve_choices()` drives them.

**Win rule** (`playloop/winrule.py`). `get_final_result(game)` reads end-of-game metrics
(score/cities/techs/population/units) from the observation at the cap. `decide_winner(A, B)` is
pure: higher primary (score) wins, tiebreak `cities → techs → population → units`; cap and primary
metric are configurable.

**Orchestrator** (`playloop/orchestrate.py`). The hard part is turn handoff without deadlock: env
calls block on the tornado IOLoop, and in a 2-player game they block on *each other*
(`gameB.reset()` waits for A's first phase; `A.end_turn()` waits for B's phase). One shared worker
thread deadlocks, so each side gets its own thread; freeciv's sequential phases serialize the
active side; a `query_lock` guarantees only one contestant is queried at a time;
`begin_turn_timeout` (120s) plus a `join` timeout turn a hang into a reported truncation; and side
A starts first (plus a delay) so A is deterministically player 0.

Orchestration results (`runs/orch-test1`, `runs/orch-test2` — both `random:1` vs `random:2`, cap 4,
seed 42, ~19s wall-clock — observed while running, not recorded in `orchestration.json`): full
2-player game with strict A↔B alternation every turn, 6–8 actions per side per
turn, **illegal 0 / stale 0 all game**. The winner path runs end to end: both scores read at the
cap, 0–0 → tiebreak on techs → winner **A**, mapped to the correct side (B's player had been
eliminated: units 0, terminated). Re-running the same seed gives an identical verdict and identical
per-player final score/techs/cities/population/units; the only difference is the losing side's final
*turn* counter (6 vs 5), a thread-handoff timing artifact `decide_winner` does not read.

### Offline test suites (no env, no model, no cost)

`pytest` from the repo root collects and runs all four (`test_winrule` 3, `test_retry` 6,
`test_orchestrate` 2, `test_ollama_think` 4): **15 tests, green**, with no CivRealm, no
numpy, no network, no Docker, no model and no API key installed (CI does exactly this on 3.10 and
3.11). Each is still runnable alone with `.venv/bin/python -m playloop.test_<name>`.

## Measured runs

Rows whose run directory is **committed** have a `summary.json` you can open at
`playloop/runs/<run>/`; the four marked *(local only)* were run on the author's machine and are
gitignored, so their numbers are reported here but cannot be checked from a clone.

| run | contestant | mode | turns | illegal rate | score | cities | cost |
|---|---|---|---|---|---|---|---|
| `main` | claude-opus-4-8 | freeform | 50 | **16.6%** (100/603) | 16 | 3 | $4.77 |
| `sonnet-baseline` | claude-sonnet-4-6 | freeform | 30 | 1.3% (3/227) | 10 | 2 | $2.02 |
| `sonnet-fixed` | claude-sonnet-4-6 | freeform, fixed repr | 30 | **0.0%** | 10 | 2 | $1.80 |
| `constrained-smoke` *(local only)* | claude-sonnet-4-6 | constrained (pre-`stale`) | 2 | 0 illegal applied (2 stale) | 1 | 1 | $0.17 |
| `constrained-smoke2` | claude-sonnet-4-6 | constrained | 2 | **0.0%** | 1 | 1 | $0.13 |
| `local-smoke` *(local only)* | llama3.2:1b | constrained | 2 | 0.0% | 0 | 0 | $0 |
| `local-llama1b-50turn` | llama3.2:1b | constrained, `format:"json"` | 50 | **0.0%** | 0 | 0 | $0 |
| `local-llama1b-schema` | llama3.2:1b | constrained, JSON Schema | 15 | **0.0%** | 0 | 0 | $0 |
| `probe-qwen-smoke` *(local only)* | qwen3:4b (`think` on) | constrained | 2 | n/a — 0 actions emitted | 0 | 0 | $0 |
| `probe-qwen-smoke2` *(local only)* | qwen3:4b (`think=False`) | constrained | 2 | 0.0% | 1 | 1 | $0 |

`runs/constrained-haiku50` and `runs/constrained-sonnet50` are abandoned 50-turn attempts (5 and 3
turns of `metrics.jsonl`, no `summary.json`) — stopped to conserve API usage, not results.

### What the Opus free-form run actually showed

50/50 turns, no crash, score 0 → 16, 3 cities (turns 1, 4, 23), units 5 → 13, and 193 unit moves /
96 terrain improvements / 67 city-production actions / 48 research actions / 3 attacks / 2 huts.
It plays; it does not no-op or end turns immediately. Turns 1–13 are near-flawless.

**92 of the 100 illegal proposals are a single confusion** (verified against the transcript): the
model reaches for `fortify` to hold a unit, but for a unit mid-activity the legal hold action is
`keep_activity`/`cancel_order`. All 92 had `keep_activity` listed in that unit's own presented
action list. The other 8 were 6 blocked-direction `goto`s plus 2 misc. It worsens late-game only
because there are more idle units to mishandle.

Framing that still holds: **illegal proposals were never executed.** The loop validates every move
against `info['available_actions']` and applies only legal ones, so gameplay was always valid. The
16.6% measures how faithfully a model copies from the presented options — a diagnostic, not bad
moves reaching the board. Free-form was left unconstrained precisely so it could surface gaps like
this one; constrained mode is the answer to the gap.

### What the Sonnet A/B showed

Annotating hold/idle actions in the representation (`keep_activity [hold: stay put]`,
`fortify [hold: dig in]`) and steering the prompt to "use the HOLD action in THAT unit's list" took
Sonnet 1.3% → 0.0% over 30 turns with no loss of play (2 cities, 138 moves, 52 terrain
improvements). No silent aliasing was added, so the rate still honestly measures adherence.

The more interesting finding: Sonnet made **zero** `fortify` mistakes on the *original*
representation — its 3 illegals were all blocked-direction `goto`s. The 16.6% was Opus
idiosyncratically defaulting to `fortify`, not a representation flaw: same representation, very
different adherence. Hence constrained selection rather than per-model
prompt tuning. The Opus projection (92/100 resolved → ~1.3%) was never re-run live; the A/B used
Sonnet to conserve usage.

### What the local-model runs showed

The harness is model-agnostic and the guarantee holds with a genuinely weak model: 50/50 turns with
`llama3.2:1b`, illegal 0.0 every turn, $0. But structure is not competence.

With `format:"json"` only (syntactic JSON, no schema) the 1B emitted valid JSON containing
hallucinated actor keys — on turn 1 its stated plan was literally "build_city" and it returned
`{"unit:102": 2, "city:110": 2}`, where option 2 for unit:102 is `plant` and `city:110` did not
exist (0 cities that turn). Adding the per-turn schema fixed the structural problems outright:

| metric (per turn) | `format:"json"` (50t) | JSON Schema (15t) |
|---|---|---|
| actors acted on (of 8 offered) | 1.0 | **7.0** (+1 explicit skip) |
| `unknown_actor` (hallucinated) | 50 | **0** |
| retries needed | 100 | **0** |
| illegal | 0 | 0 |

The residual gap is 1B judgment, not format: it now makes legal, complete choices and poor ones —
it moves, improves terrain, sets research and rates, and never picks `build_city` for its Settlers
(0 cities, score 0 over 15 turns). Told directly to "pick option 4 (build_city)" it returned a
schema-valid `0` (skip). Competent local play needs a bigger model.

`qwen3:4b` is that next step and needed one fix: 4B "thinking" models spend the whole
`num_predict` budget on the hidden `message.thinking` channel and return **empty**
`message.content` (`done_reason=length`) — no answer at all. `probe-qwen-smoke` shows the failure
(`ollama: no JSON parsed`, 0 actions, both turns). With `think=False` (now the default; harmless on
non-thinking models like `llama3.2:1b`) `probe-qwen-smoke2` founds a city and applies 11 actions
over 2 turns. `test_ollama_think` locks the payload shape in with a fake transport.

### Ollama / MLX, verified on this machine

Ollama is cross-platform (llama.cpp/GGUF; Metal on macOS, CUDA/CPU elsewhere), which is why the
Apple-only MLX prototype was dropped. **MLX is not used for GGUF text models here:** `llama3.2:1b`
loads the GGML/Metal runner (`ggml_metal_init`, "offloaded 17/17 layers to GPU" on an M3) —
fully GPU-accelerated, but not MLX. `OLLAMA_NEW_ENGINE=1` does not switch a GGUF text model to MLX
(tested). This build ships MLX (`mlx_metal_v3/v4`, an `mlxrunner`) for image-generation and
MLX-format models, not GGUF text inference.

## Not done / known limitations

- **No two-LLM head-to-head.** The orchestrator is verified only with random contestants. Also
  absent: brackets, Elo or any rating, multi-game fairness.
- **No capable-model constrained 50-turn run.** Constrained smokes founded a city by turn 2 like
  the free-form runs, suggesting no regression, but a full constrained-vs-free-form comparison on a
  capable model has not been run. Cheapest path now is a 7–8B local model.
- **Emulation is slow.** The amd64 image runs under emulation on arm64: `reset()` takes ~10-30s and
  a turn a few seconds. No arm64 image is published.
- **Stale game on a port.** If `reset()` crashes mid-connection the in-container civserver for that
  `client_port` can stick, and the next `start_game` may `BeginTurnTimeout`. Workaround: restart the
  container, or use a different `client_port` (6001 or 6300-6331). No auto-recovery.
- **Large observations.** `get_observation()` with no `keys` includes the `map` arrays (a
  `(X, Y, 52)` unit array ≈ 200k ints). Pass `keys=["unit","player","city",...]`.
- **Winner detection.** `terminated` covers defeat and game-over flags; the per-player `winner`
  appears in `get_status` only once the server sends the endgame packet. Mid-game victory-condition
  termination for one's own player is not implemented upstream (`# FIXME: check victory
  conditions.` in civrealm). The score-based win rule at a turn cap exists because of this.
- **Single client, single env.** The server owns one env and serializes tool calls on one worker
  thread — right for one driver, not built for concurrent clients.
- **No `llm_info`.** The server wraps `FreecivBase-v0` (raw obs); the human-readable
  `info['llm_info']` from `FreecivLLM-v0`/`LLMWrapper` is not exposed.

## Next steps, in order

1. **A real head-to-head:** swap `RandomContestant` for two model contestants in
   `orchestrate.py` — the slot is already pluggable and the win rule already decides. Cheapest
   first pass is `qwen3:4b` vs `qwen3:4b` (free, local, now that `think=False` works).
2. **A capable constrained 50-turn baseline** for the constrained-vs-free-form comparison.
3. **Lifecycle hardening:** auto-pick a free `client_port`, detect and restart stuck ports,
   optionally manage the Docker container from the server.
4. Only then rating/brackets — none of which belong in the single-env MCP server.

## Reproduce

Docker server up first (see `SETUP.md`) for everything that touches a game; the offline suites
need neither Docker nor a model.

```bash
# the MCP path
.venv/bin/python tests/acceptance_test.py

# play loop: claude backend, constrained mode (both defaults)
.venv/bin/python -m playloop.loop --max-turns 50
.venv/bin/python -m playloop.loop --backend local --model qwen3:4b --max-turns 15   # needs Ollama

# two sides, one game, decided winner
.venv/bin/python -m playloop.orchestrate --cap 6 --seed 42 --side-a random:1 --side-b random:2

# offline suites (free, no env): winrule / retry / orchestrate / ollama_think
.venv/bin/python -m playloop.test_winrule
```

## Changelog

| date | commit | change |
|---|---|---|
| 2026-06-14 | `89f8c86` | Minimal MCP server over a single CivRealm env, plus `SCHEMA.md`, `SETUP.md`, acceptance test |
| 2026-06-15 | `a45efdf` | Single-agent play loop (viability probe) + `core.py` shared env layer; MCP acceptance re-run green after the refactor |
| 2026-06-15 | `5013c53` | Hold-action representation fix + Sonnet 4.6 A/B (1.3% → 0.0%) |
| 2026-06-15 | `2fdeca5` | Constrained action selection + score-based win rule |
| 2026-06-15 | `a4bccc5` | Retry-feedback loop + cross-platform local contestant (Ollama) |
| 2026-06-15 | `4105025` | Local 1B 50-turn run + Ollama/MLX findings |
| 2026-06-15 | `1690e77` | Structured-output enforcement: per-turn JSON Schema, one enum per actor |
| 2026-06-15 | `b49c51f` | `test_retry`: accept the schema kwarg in `MockContestant` |
| 2026-06-15 | `69cd5f6` | Two-agent orchestration: two contestants, one shared game, decided winner (random contestants) |
| 2026-06-15 | `2f93f69` | Fix 4B thinking models returning empty output (`think=False`) |
| 2026-06-15 | `e4c4732` | `--seed` for reproducible games + per-turn techs logging |
