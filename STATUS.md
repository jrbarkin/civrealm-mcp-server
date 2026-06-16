# STATUS

Exploratory first pass: a thin MCP server exposing **one** CivRealm (Freeciv) environment.
All four phases of the task were completed and verified by **running** them on macOS (Apple
Silicon, arm64) against the Dockerized freeciv-web server. Output was pasted at each gate.

_Last verified: 2026-06-14._

---

## What works (verified, with pasted evidence)

### Phase 0 — Environment
- **Python env**: Python 3.11 venv (via `uv`) with `civrealm==0.1.2` and pinned deps
  (`gymnasium 0.29.1`, `selenium 4.9.1`, `tornado 6.3.2`, `ray 2.6.3`, `numpy 2.4.6`).
  Fix required: `setuptools<81` (ray imports the removed `pkg_resources`).
- **Docker server**: `civrealm/freeciv-web:latest` (amd64) pulled & retagged; container runs
  under emulation on arm64. `curl http://localhost:8080/` → **HTTP 200**.
- **0.4 single-player smoke test** (`test_civrealm --max_turns=2`): **PASS** —
  `Reset with port: 6001`, units acting (`('unit', 101, 'build_city')`, `goto_*`), turn
  advance, `Truncated: True` at turn 3, and a 4-player `game results` dict.
- **0.5 two-player smoke test** (`--minp=2 --username=myagent` + `--username=myagent1`, same
  `client_port=6001`): **PASS, provably one game** — each client's log shows the *other*
  player connecting (`'myagent1 has connected ... (player Myagent1)'` and vice-versa), both on
  `challenge_6001_*` with identical `mapseed 681897 / gameseed 950276`, and **both printed an
  identical final 4-player `game results`** (impossible across two separate games).

### Phase 1 — Schema
- **[SCHEMA.md](SCHEMA.md)** documents the observation dict (11 top-level keys: game, rules,
  map, player, city, tech, unit, options, dipl, gov, client), the `info` dict
  (`turn`, `available_actions`, optional `llm_info`), the action 3-tuple
  `(ctrl_type, actor_id, action_name)` with `None`=end-turn, and reward/terminated/truncated/
  winner signalling. Derived from source + a real **unpickled** observation + the live runs.

### Phase 2 — MCP server
- **[src/civrealm_mcp/server.py](src/civrealm_mcp/server.py)** (official `mcp` SDK / FastMCP,
  stdio) owns ONE `FreecivBaseEnv` and exposes the 7 required tools: `start_game`,
  `get_observation`, `get_legal_actions`, `take_action`, `end_turn`, `get_status`,
  `close_game`. No LLM in the server (model-agnostic). Observations/actions serialized to JSON.

### Phase 3 — Acceptance test
- **[tests/acceptance_test.py](tests/acceptance_test.py)** spawns the server over stdio (a real
  MCP client) and runs `start_game → get_observation → get_legal_actions → take_action →
  end_turn → get_status → close_game` with **no GUI / no direct civrealm calls**. **PASS** —
  e.g. `take_action(unit, 104, build_city)` then `end_turn` advanced turn 1→2 with
  `last_reward: 1` (score 0→1), and `Reset with port: 6001` correctly went to **stderr**
  (proving the JSON-RPC stdout stays clean).

## Bugs found & fixed during bring-up (TDD: failing run → fix → passing run)
1. **`pkg_resources` missing** (ray 2.6.3 on setuptools≥81) → pin `setuptools<81`.
2. **Apple-Silicon port conflict**: default port map publishes `7000-7009`, but macOS Control
   Center (AirPlay Receiver) owns port 7000 → publish only `8080:80` (the only host port the
   client needs). The acceptance test connecting successfully is the regression check.
3. **`This event loop is already running`**: CivRealm drives a tornado IOLoop inside
   `reset()/step()`, which collides with FastMCP's asyncio loop → offload all env calls to a
   single dedicated worker thread. The acceptance test (which failed on this, then passed after
   the fix) is the regression test.
4. **`BeginTurnTimeout` in the server**: don't override `multiplayer_game`; raise
   `begin_turn_timeout` to 120s for the emulated server. Fixed; acceptance test passes.

## What does NOT work / known limitations
- **Emulation is slow**: the amd64 image runs under qemu/Rosetta on arm64; `reset()` takes
  ~10-30s and a turn a few seconds. No arm64 image is published.
- **Stale game on a port**: if `reset()` crashes mid-connection, the in-container civserver for
  that `client_port` can get stuck and the next `start_game` may `BeginTurnTimeout`. Workaround:
  restart the container (`docker stop freeciv-web`), or use a different `client_port`
  (6001 or 6300-6331). The server does not yet auto-recover stuck ports.
- **Large observations**: `get_observation()` with no `keys` includes the `map` arrays
  (e.g. a `(X, Y, 52)` unit array → ~200k ints). Use `keys=["unit","player","city",...]`.
- **Winner detection**: `terminated` covers defeat / game-over flags; the per-player `winner`
  appears in `get_status` only once the server sends the endgame packet (true game end).
  Mid-game victory-condition termination for *your* player is not implemented upstream
  (`# FIXME: check victory conditions.` in civrealm).
- **Single client, single env**: the server owns one env and serializes tool calls on one
  worker thread (correct for one driver; not built for concurrent clients).
- **No `llm_info`**: the server wraps `FreecivBase-v0` (raw obs). The human-readable
  `info['llm_info']` from `FreecivLLM-v0`/`LLMWrapper` is not exposed yet.

## Recommended next step
Build the **two-agent orchestrator** (out of scope here, but the foundation is proven): run two
MCP-driven clients against the **same** `client_port` with `minp=2` and distinct usernames
(0.5 showed two clients share one game), and an external loop that alternates each LLM's turn
via the existing tools. Concretely:
1. Add a `start_game(multiplayer=True, minp=2, username=...)` path (or a small `join_game`
   variant) so two server instances can join one game on one port.
2. Add an optional `llm_info` surface (wrap `FreecivLLM-v0`) for cheaper natural-language
   observations.
3. Harden lifecycle: auto-pick a free `client_port`, detect/restart stuck ports, and optionally
   manage the Docker container from the server.
4. Only then layer on turn arbitration, then rating/bracket — none of which belong in this
   single-env MCP server.

## How to reproduce
See **[SETUP.md](SETUP.md)** for exact commands. Short version: start the Docker server
(`docker run -d --name freeciv-web --rm -p 8080:80 freeciv/freeciv-web:latest`), then
`.venv/bin/python tests/acceptance_test.py`.

---

# Update (2026-06-15): Single-agent play loop — can one LLM actually play?

A second task: a headless loop that plays ONE game vs the built-in AI by looping
observe → shape state + legal actions → ask an LLM for moves → apply the legal ones → end turn,
until a turn cap. This is a **viability probe** (coherence, not winning).

## What was built
- **Shared engine** `src/civrealm_mcp/core.py` (`CivRealmGame`) — the MCP server was refactored
  to drive through it, so the play loop and the MCP server use **one env-access layer**. (The MCP
  acceptance test was re-run after the refactor — still passes, no regression.)
- **Shaping layer** `playloop/representation.py` + **[REPRESENTATION.md](REPRESENTATION.md)** +
  a real `EXAMPLE_SHAPED_STATE.txt`. Condenses the raw observation into a compact strategic
  summary + your cities/units + per-actor **exact legal actions** (cryptic `goto_*` keys annotated
  with compass directions). Raw→shaped stays debuggable (full raw snapshot saved for turn 1).
- **Contestant** `playloop/contestant.py` — one configurable slot. Instead of the paid API it
  **spawns an isolated `claude` subagent per turn** (`claude -p ... --output-format json`,
  tools disabled), using this environment's auth. Probe model: **Claude Opus 4.8** (strong, so a
  flail implicates our representation, not the model).
- **Loop + metrics + transcript** `playloop/loop.py` → `playloop/runs/<ts>/{metrics.jsonl,
  transcript.json,summary.json}`.

## The real run (50 turns, Opus 4.8, `playloop/runs/main/`)
Acceptance: **complete the cap without crashing**, **illegal-action rate < 15%**, **demonstrably
playing**.

| metric | result |
|---|---|
| turns completed | **50 / 50, no crash** ✅ |
| score | **0 → 16** |
| cities founded | **3** (turns 1, 4, 23) |
| units | 5 → 13 |
| demonstrably playing | 193 unit moves, 96 terrain improvements, 67 city-production actions, 48 research actions, 3 cities, 3 attacks, 2 huts entered ✅ |
| illegal-action rate | **16.6% overall** (100/603), mean per-turn **14.5%** — borderline vs <15% |
| cost | ~$4.77 (50 subagent calls) |

**Honest read — is it playing or flailing? Playing, clearly.** It expands, improves terrain,
manages city production, and steers research across all 50 turns; the early game is near-flawless
(turns 1–13: ~0 illegal). It does *not* no-op or end turns immediately.

## The single biggest obstacle (and the design point)
**92 of 100 illegal proposals are one confusion:** the model reaches for `fortify` to "hold" a
unit, but for units mid-activity the legal hold action is `keep_activity`/`cancel_order`, not
`fortify`. (6 were blocked-direction `goto`s; 2 misc.) It worsens late-game purely because there
are more idle units to mishandle — the documented "control many units" difficulty, narrowed to
one fixable semantic gap. **0 illegal proposals targeted a non-actionable actor** — the model is
not commanding dead units.

Important framing: **illegal proposals are never executed** — the loop validates every move
against `info['available_actions']` and only applies legal ones, so gameplay is always valid. The
16.6% is a *diagnostic of how faithfully the model copies from the presented valid-options list*,
not bad moves hitting the board. The choice was left unconstrained on purpose so the probe could
*find* representation gaps like this; a production head-to-head would instead **constrain
selection to the presented options** (enum/numbered choice → illegal rate structurally 0).

Two concrete fixes, both validated against the captured data (`all 92 fortify-illegals had
keep_activity listed`):
1. **Map the hold action**: alias `fortify → keep_activity` when fortify isn't legal (and label
   the hold option clearly in the representation). Applying this to the run's data resolves 92/100
   → **projected illegal rate 1.3%** (8/603), well under target.
2. **Constrain selection** to the offered keys (the env layer already supports this via
   `available_actions`).

Neither was applied here (the task says stop and report after the run); both are one-evening changes.

## Deliverables (this task)
`src/civrealm_mcp/core.py`, `playloop/{representation,contestant,loop}.py`,
`REPRESENTATION.md`, `EXAMPLE_SHAPED_STATE.txt`, and the real run under `playloop/runs/main/`
(`metrics.jsonl` + `transcript.json` + `summary.json`).

## Reproduce
Docker server up (see SETUP.md), then:
`.venv/bin/python -m playloop.loop --max-turns 50` (or a smaller cap to validate quickly).

---

## Follow-up (2026-06-16): hold-action fix + cheaper-model A/B

Applied the representation fix and re-tested with **Claude Sonnet 4.6** (the contestant is a
`--model` flag). The fix is principled, not leniency: `representation.py` now annotates the
hold/idle actions (`keep_activity [hold: stay put]`, `fortify [hold: dig in]`, …) so the listed
hold action is unambiguous, and the contestant prompt steers to "use the HOLD action in THAT
unit's list; don't use fortify unless it's listed." No silent aliasing was added, so the
illegal-action rate still honestly measures adherence to the presented options.

A/B (illegal-action rate; all runs completed the cap, no crash, demonstrably playing):

| run | turns | illegal rate | fortify-illegals | score | cities |
|---|---|---|---|---|---|
| Opus 4.8, original representation | 50 | **16.6%** | 92 | 16 | 3 |
| Sonnet 4.6, original representation | 30 | **1.3%** | 0 | 10 | 2 |
| Sonnet 4.6, **fixed** representation | 30 | **0.0%** | 0 | 10 | 2 |

**Two findings:**
1. **The illegal-action problem was model-specific, not a representation flaw.** Sonnet 4.6 made
   *zero* `fortify` mistakes on the *original* representation (1.3% overall, all minor) — the
   16.6% was Opus 4.8 idiosyncratically defaulting to `fortify`. Same representation, very
   different adherence. This matters for the eventual head-to-head: given identical valid-options,
   contestants differ in how faithfully they pick from the list, so the production design should
   constrain selection (enum/numbered choice) and/or tune per model — not assume one prompt fits all.
2. **The hold-action fix helps and doesn't regress.** Sonnet went 1.3% → **0.0%** with the fix,
   still founding cities, moving units (138), and improving terrain (52) over 30 turns.

Per the "no Opus for testing" constraint this A/B used Sonnet; the projected Opus improvement
(92/100 illegals resolved → ~1.3%) remains a data-derived projection, not a live Opus re-run.

Runs: `playloop/runs/sonnet-baseline/` and `playloop/runs/sonnet-fixed/`.
Reproduce a cheap run: `.venv/bin/python -m playloop.loop --model claude-sonnet-4-6 --max-turns 30`.

---

# Update (2026-06-15): Constrained selection + win rule + retry feedback + local-LLM contestant

Preconditions for a fair head-to-head (no orchestration yet).

## Part A — constrained action selection (illegal action impossible by construction)
- `representation.build_choice_menu()/render_menu()`: a per-actor NUMBERED menu of that actor's
  legal actions (keyed `unit:104`/`city:110`/`tech:cur_player`/`gov:0`; option `0` = skip). The
  contestant returns an option NUMBER per actor, so the action strings never come from the model.
  Grouped per actor (not a flat 300-item list); one model call per turn.
- `loop.py --mode constrained` (default): `apply_constrained` resolves numbers via the menu index
  and applies only legal actions. Counters: applied / skipped / bad_index / unknown_actor /
  duplicate / `stale` (legal at menu-build but changed by an earlier same-turn action under stacked
  units — benign, skipped). **Illegal applied actions are 0 by construction.**
- Verified (Sonnet 4.6 constrained smoke, `playloop/runs/constrained-smoke2/`): illegal_action_rate
  **0.0**, selection_error_total **0**, all attempted choices applied; `final_result` read at the cap.
  See `EXAMPLE_CONSTRAINED_MENU.txt` for a real per-actor menu. (A full 50-turn paid run was
  deprioritized to conserve usage — use the local-LLM path below.)

## Retry-feedback loop ("an illegal attempt should throw + remind the agent of the options")
- When a selection is invalid (bad option number / unknown actor key), the loop returns an error
  that **enumerates the acceptable options** for those actors and retries, merging only the
  corrected actors, up to `max_retries`. `invalid_selections()` and `build_feedback()` are pure;
  `resolve_choices()` drives it.
- Verified by `playloop/test_retry.py` (mock contestant, **zero usage**): bad index + unknown actor
  are fed back with valid options, the correction is merged, valid selections preserved, an
  uncorrected one stays flagged, happy path does no retries. **12/12 pass.**

## Part B — score-based win rule
- `winrule.get_final_result(game)`: end-of-game metrics (score/cities/techs/population/units) read
  from the observation at the cap. PURE `decide_winner(A, B)`: higher primary (score) wins; tiebreak
  `cities → techs → population → units`; cap + primary metric configurable.
- `playloop/test_winrule.py`: **8/8 pass** (A>B, B>A, each tiebreak level, full tie, configurable
  primary, missing keys default 0). The loop logs `final_result` + `win_rule` at game end.

## Local-LLM contestant (cross-platform, zero API usage)
- `contestant.OllamaContestant`: a small local model via **Ollama** (llama.cpp/GGUF core →
  macOS/Linux/Windows), default **`llama3.2:1b`** (Llama-3.2-1B-Instruct), using Ollama's
  `format="json"`. Selected with `--backend local`. The contestant slot is pluggable:
  `claude` subagent | local Ollama.
- **Status: coded + import-verified; the local game has NOT been run here** — the Ollama server came
  up (v0.30.8) but the model pull was interrupted and Ollama was then uninstalled (to be set up on
  your side). Run it after installing Ollama (see SETUP.md):
  `ollama pull llama3.2:1b` then `.venv/bin/python -m playloop.loop --backend local --max-turns 50`.
  Constrained mode keeps illegal=0 regardless of model; the retry loop recovers a 1B's malformed
  selections.

## Verified vs pending
- **Verified** (no/low usage): win-rule tests 8/8, retry tests 12/12, constrained smoke
  illegal_rate 0.0, MCP acceptance still green after the `core.py` refactor.
- **Pending (your machine):** the local 50-turn game with Ollama + `llama3.2:1b` (command above);
  optionally a full 50-turn paid constrained run for a paid-model baseline.
