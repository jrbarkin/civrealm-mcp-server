# civrealm-mcp-server

[![tests](https://github.com/jrbarkin/civrealm-mcp-server/actions/workflows/tests.yml/badge.svg)](https://github.com/jrbarkin/civrealm-mcp-server/actions/workflows/tests.yml)

Two things live in this repo:

1. **An MCP server** (`src/civrealm_mcp/`) that exposes a single
   [CivRealm](https://github.com/bigai-ai/civrealm) (Freeciv) Gymnasium environment as **seven
   [Model Context Protocol](https://modelcontextprotocol.io) tools** over stdio, so any MCP client
   — Claude Desktop, a script, a person poking at it by hand — can drive one game with no GUI and
   no LLM inside the server.
2. **A headless play-loop harness** (`playloop/`) that runs **one LLM** through a capped game turn
   by turn under **per-turn constrained action selection**, and records metrics, transcripts and
   summaries per run. This is where most of the code and all of the experimental results are.

The two halves share one env-access layer (`src/civrealm_mcp/core.py`) so the debug surface and the
experiment can't drift. **The play loop does not go over MCP** — it imports `civrealm_mcp.core`
directly, because a blocking tornado IOLoop per env call is easier to drive in-process than across
a JSON-RPC transport. MCP is the inspection/interop surface; the harness is the experiment.

This is exploratory research code, not a product.

## Headline result: 16.6% → 0 illegal actions

The measurable finding is about **how a model selects from a large legal-action set**, not about
Freeciv skill.

| run | contestant | turns | mode | illegal-action rate |
|---|---|---|---|---|
| `playloop/runs/main` | Claude Opus 4.8 | 50 | free-form action strings | **16.6%** (100/603) |
| `playloop/runs/sonnet-baseline` | Claude Sonnet 4.6 | 30 | free-form | 1.3% (3/227) |
| `playloop/runs/sonnet-fixed` | Claude Sonnet 4.6 | 30 | free-form + hold-action annotation | 0.0% (221/221 applied) |
| `playloop/runs/constrained-smoke2` | Claude Sonnet 4.6 | 2 | constrained menu | **0.0%** (11/11) |
| `playloop/runs/local-llama1b-50turn` | llama3.2:1b (Ollama) | 50 | constrained menu, JSON-mode only | **0.0%** |
| `playloop/runs/local-llama1b-schema` | llama3.2:1b (Ollama) | 15 | constrained menu + per-turn JSON Schema | **0.0%** |

In free-form mode the model returns action strings and the loop validates each one against
`info['available_actions']`; illegal proposals are **counted and dropped, never executed**, so
16.6% measures how faithfully the model copied from the list it was shown — not bad moves landing
on the board.

### The controlled pair: `playloop/runs/ab-opus48-50t`

The rows above compare across models and run lengths. This one does not. Same model
(`claude-opus-4-8`), same length (50 turns), same seed within each pair, same backend
(`--backend api`, where the schema constrains the decoder) — **`--mode` is the only difference**.
Pre-registered in
[`playloop/runs/ab-opus48-50t/PREREGISTRATION.md`](playloop/runs/ab-opus48-50t/PREREGISTRATION.md),
committed in `62ad71e` **before the first run**.

| seed | mode | unusable selections | applied | actors never addressed | score | retries |
|---|---|---|---|---|---|---|
| 42 | free-form | **13.62%** (88/646) | 558 | 19.0% | 11 | 0 |
| 42 | constrained | **0.0000%** (0/899) | 797 | 0% | 12 | 0 |
| 43 | free-form | **17.50%** (109/623) | 514 | 17.2% | 13 | 0 |
| 43 | constrained | **0.0000%** (0/973) | 864 | 0% | 10 | 0 |

*Unusable selections* is the pre-registered primary metric: the fraction of the model's per-actor
decisions naming something not on offer. It is measured in each arm's own units — free-form
`(illegal + malformed)`, constrained `(bad_index + unknown_actor)` — because `illegal` is a
structural 0 in constrained mode and quoting it would be circular. **The denominators differ and
the comparison is therefore not exact**: free-form counts only decisions the model volunteered,
constrained counts every actor, because the schema requires an answer for each. That caveat was
written down before the runs, not after.

Read together: **zero unusable selections in 1,872 constrained decisions across both seeds, against
13.6% and 17.5% free-form.** None of the four pre-registered conditions for "the constraint did not
help" fired.

Three things worth more than the headline:

- **The schema stops omission, not just illegality.** The free-form arm silently never answered for
  17-19% of the actors it was shown. The constrained arm answered for all of them — `required` in
  the schema is doing as much work as `enum`.
- **The `fortify` story replicates, but it is only half the story.** README used to attribute the
  bulk of free-form errors to Opus reaching for `fortify` when the legal hold was `keep_activity`;
  that was 92 of 100 in `runs/main`, and 92 of 197 here. The other near-half is illegal `goto_*`
  (90 of 197) — moving in a direction that is not legal from that tile. Two failure modes, not one.
- **Constraint did not buy a better game.** Score 12 vs 11 on seed 42 and 10 vs 13 on seed 43 —
  mixed, no effect worth claiming. This experiment is about output legality, not Freeciv skill.

Honest limits: two seeds, one run each, one model. Both seeds agree, but this is not a variance
estimate. Reaching 50 turns on the seed-43 constrained arm took three attempts — the first was
killed externally at turn 46, the second ran out of API credit at turn 26. Both failed attempts are
committed next to the result (`constrained-seed43-interrupted`, `constrained-seed43-credit-exhausted`)
with notes, because the pre-registration said no run would be discarded.

Constrained mode replaces strings with **option numbers from a per-actor numbered menu of that
actor's legal actions** (`representation.build_choice_menu`), and `menu_to_schema` turns that menu
into a **per-turn JSON Schema** — one integer property per actor, each an `enum` of that actor's
valid option numbers, all actors `required`, `additionalProperties: false`. Every constrained run
since the counters were fixed measures `illegal_action_rate` **0.0**.

Two different things produce that 0.0, and the table below is only honest if they are kept apart:

1. **The loop cannot execute an unoffered action, on any backend.** Choices are option *numbers*,
   resolved back through `menu.index` before `take_action` — so "illegal" is 0 by construction,
   not by measurement. `apply_constrained` returns a literal `0` for it, and says so at the code
   site.
2. **Whether the *model* can even emit a bad number depends on the backend**, and that is the
   part worth being precise about:

| backend | how the same schema is applied | can an out-of-enum value be emitted? |
|---|---|---|
| `--backend local` (Ollama) | wire `format` field → llama.cpp compiles it to a grammar | **No** — it cannot be sampled |
| `--backend api` (Anthropic Messages API) | `output_config.format` → schema compiled server-side | **No** — see the check below |
| `--backend claude` (**default**, `claude` CLI) | `--json-schema` → the CLI validates the answer and retries | **Yes** — it is emitted, then rejected |

The distinction was measured, not assumed. Prompted to pick an out-of-enum option, the CLI
backend's model emitted `{"unit:101": 99}`; the CLI answered *"Output does not match required
schema: /choices/unit:101: must be equal to one of the allowed values"* and the turn ended with
`structured_output: null`. Against the API backend the same conflict cannot arise: with each
actor's enum narrowed to `[1]` and the prompt ordering *"answer 7 for every single actor"*, all
8 actors came back `1`, three runs out of three.

So **"unrepresentable rather than unlikely" is true of `--backend local` and `--backend api`, and
not of the default `--backend claude`**, where it is "rejected rather than executed". The default
is still the CLI because it needs no API key and it is what the committed runs were produced on.

Three honest caveats:

- **One class of failure survives constraint, and it isn't the model's.** An option can be legal
  when the menu is built and invalid by the time it is applied, because an earlier action *that
  same turn* moved a stacked unit. The first constrained smoke logged those as illegal (rate
  0.222); they are now counted separately as `stale`, applied-count only, and they still occur at a
  low rate (1 of 12 in the qwen3:4b smoke). "0 illegal" means "the model never picked an option it
  wasn't offered", not "every selection landed".

- **Part of the 16.6% was model-specific.** 92 of the 100 illegal proposals were Opus reaching for
  `fortify` to hold a unit when the legal hold action was `keep_activity`. Sonnet 4.6 made zero
  such mistakes on the *same* representation (1.3% overall). So the gap that constraint closes is
  real but was much wider for one model than another.
- **The default backend is the weakest of the three, and it is the one behind the six rows above.**
  Every committed *Claude CLI* run predates the `--json-schema` wiring, so on those runs the schema
  was built and then discarded, and their "0 illegal" rests entirely on the loop resolving option
  numbers through the menu index. The schema now reaches the CLI, but as validate-and-retry (table
  above). The decoder-enforced claim rests on `runs/ab-opus48-50t` (`--backend api`) and on the
  local Ollama runs — not on the default path.

A secondary result, from the same runs: the schema fixed *selection* quality for a weak model.
With `format:"json"` only, `llama3.2:1b` hallucinated actor keys on all 50 turns and triggered 100
retries while acting on ~1 of ~8 actors per turn; with the schema, 0 hallucinated actors, 0
retries, and ~7 actors acted on per turn. It still founded 0 cities and scored 0 — the residual gap
is 1B judgment, not output format.

## Part 1 — the MCP server

Seven thin pass-through tools over one `civrealm/FreecivBase-v0` env:

| tool | what it does |
|---|---|
| `start_game(client_port=6001, max_turns=1000, username="myagent")` | reset a single-player game vs built-in AIs |
| `get_observation(keys=None)` | current observation as JSON (optionally a subset of top-level keys) |
| `get_legal_actions(ctrl_type=None, actor_id=None)` | the legal-action mask `{ctrl_type:{actor_id:{action_name:valid}}}` |
| `take_action(ctrl_type, actor_id, action_name)` | perform one legal action (`env.step((ctrl_type, actor_id, action_name))`) |
| `end_turn()` | end the current turn (`env.step(None)`) |
| `get_status()` | turn, per-player scores, terminated/truncated, winner & final results when ended |
| `close_game()` | disconnect the env (`env.close()`) |

It reimplements no game logic. The CivRealm observation/action/lifecycle contract is documented in
**[SCHEMA.md](SCHEMA.md)**.

**This surface is only partly constrained, and it is a different guarantee from the play loop's.**
`ctrl_type` is a genuinely closed set, so it is typed `Literal["unit","city","player","dipl",
"tech","gov"]` and FastMCP renders it as an `enum` in the tool's `inputSchema` — a client cannot
offer a bad one. `actor_id` and `action_name` cannot be closed: which actors exist and which
actions are legal changes every step. They are checked *after* the call, against
`info['available_actions']`, and an illegal `take_action` returns an error plus that actor's valid
actions. So over MCP an illegal action is **rejected**; in the play loop's constrained mode
(`--backend local` / `--backend api`) it is **unrepresentable**. The per-turn JSON Schema belongs
to the harness, not to these tools.

Two implementation details that matter (both found by running it):

- **stdio hygiene:** CivRealm `print()`s to stdout; stdio MCP uses stdout for JSON-RPC. The server
  routes all non-protocol output to stderr and hands only the real stdout to the transport.
- **event loop:** CivRealm drives a tornado `IOLoop` inside `reset()/step()`; FastMCP runs tools in
  the asyncio loop. The server offloads every env call to one dedicated worker thread. (The play
  loop has no running event loop and calls the same core synchronously.)

## Part 2 — the play loop (`playloop/`)

One LLM, one game, capped at N turns. Each turn:

```
observe -> shape state -> build per-actor legal-action menu (+ JSON Schema)
        -> ask the contestant -> validate -> apply legal actions -> end turn
```

- **`representation.py`** — turns a raw observation (hundreds of keys, large numpy map arrays) into
  a compact text state plus the numbered per-actor menu and its JSON Schema. Design notes in
  **[REPRESENTATION.md](REPRESENTATION.md)**; real samples in `EXAMPLE_SHAPED_STATE.txt` and
  `EXAMPLE_CONSTRAINED_MENU.txt`.
- **`contestant.py`** — the pluggable decision-maker behind one interface
  (`choose` / `choose_constrained`): `ClaudeSubagentContestant` (spawns `claude -p` per turn, uses
  existing CLI auth, no API key), `AnthropicSDKContestant` (Messages API, needs
  `ANTHROPIC_API_KEY`, the one Claude path where the schema constrains the decoder),
  `OllamaContestant` (local model, zero API usage; `think=False` is required for 4B thinking
  models or they burn the whole budget on hidden reasoning and return empty content), and
  `RandomContestant` (seeded, model-free test fixture). Which of them enforces the schema how is
  the table above, and is spelled out in each class docstring.
- **`loop.py`** — the driver: applies selections, counts
  applied/illegal/skipped/duplicate/stale/bad_index/unknown_actor, and runs the retry-feedback loop
  that re-sends the menu with the valid option numbers enumerated when a selection can't be
  applied. Writes `metrics.jsonl`, `transcript.json`, `summary.json` per run.
- **`winrule.py`** — a real Freeciv victory takes far more turns than any practical cap, so capped
  games are decided on in-game **score** at the cap, tiebreak `cities → techs → population →
  units`. `decide_winner` is pure and unit-tested.
- **`orchestrate.py`** — two contestants sharing **one** game (`minp=2`, one thread per side, a
  query lock so only one brain is asked at a time, and a `begin_turn_timeout` so a stuck handoff
  truncates instead of deadlocking). See the scope note below for what has actually been run
  through it.

Contestants are selected with flags, not code changes:

```bash
.venv/bin/python -m playloop.loop --max-turns 50                          # Claude Opus 4.8, constrained
.venv/bin/python -m playloop.loop --model claude-sonnet-4-6 --max-turns 30
.venv/bin/python -m playloop.loop --backend local --model llama3.2:1b --max-turns 15
.venv/bin/python -m playloop.loop --backend api --max-turns 50            # decoder-enforced schema
.venv/bin/python -m playloop.loop --mode freeform --max-turns 50          # the unconstrained baseline
```

`--backend api` reads `ANTHROPIC_API_KEY` from the environment and reports no per-turn cost in
dollars, so a run's `total_cost_usd` reads 0 on it. What it does record is the usage the API
returns: `input_tokens`/`output_tokens` per turn in `metrics.jsonl`, summed into
`summary.json`'s `total_input_tokens`/`total_output_tokens`. Multiply by the rate on the day —
the price is not a property of the run, and baking one in would put a number in the output that
no committed artifact backs.

Run outputs land in `playloop/runs/<timestamp>/` (or `--out-dir`). `playloop/runs/` is gitignored
apart from the eight directories committed as evidence: `main`, `sonnet-baseline`, `sonnet-fixed`,
`constrained-smoke2`, `local-llama1b-50turn`, `local-llama1b-schema`, `orch-test1`, `orch-test2`.
Other run directories mentioned in this README — the qwen3:4b smokes, the earlier 2-turn smokes,
and the two aborted 50-turn constrained attempts — exist only on the machine that produced them.

## Scope — what this repo is *not*

- **There has never been a two-LLM head-to-head.** `orchestrate.py` is verified end-to-end only
  with two seeded `RandomContestant`s (`playloop/runs/orch-test1`, `orch-test2`:
  `side_a.spec="random:1"` vs `side_b.spec="random:2"`, cap 4, seed 42, same seed → identical
  verdict). No model has played either side. The mechanism — shared game, sequential phases, no
  deadlock, a decided winner — works; the tournament does not exist.
- **No tournament, bracket, Elo or rating.** Nothing in here ranks anything.
- **~~No capable-model 50-turn constrained run.~~** Closed by `runs/ab-opus48-50t`: two 50-turn
  Opus 4.8 constrained runs, decoder-enforced, committed. The older aborted attempts
  (`constrained-sonnet50`, `constrained-haiku50`: 3 and 5 turns of metrics, no `summary.json`)
  remain what they were — not results, and not in the tree.
- **No verified victory path.** Every capped run ends `truncated` at the turn cap, which is why
  the score-based win rule exists at all. The only `terminated` ever observed was a random
  contestant's player being *eliminated* mid-orchestration-test — never a win condition.
- **Single-player-vs-built-in-AI is the only shape the MCP server offers.** The two-player setup
  lives in `orchestrate.py`, not in the tools.

## Quick start

Full steps, including the macOS/Apple-Silicon fixes, are in **[SETUP.md](SETUP.md)**. In short:

```bash
# 1. freeciv-web Docker server (publish only 8080 — avoids the macOS AirPlay :7000 conflict)
docker run -d --name freeciv-web --rm -p 8080:80 freeciv/freeciv-web:latest
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/        # -> 200

# 2. Python 3.11 env with civrealm + this server (see SETUP.md for the setuptools<81 fix)
uv venv --python 3.11 .venv
uv pip install --python .venv -e /path/to/civrealm 'setuptools<81'
uv pip install --python .venv --no-deps -e .

# 3. drive a game purely through MCP tools
.venv/bin/python scripts/acceptance_check.py

# 4. or run the play loop (needs `claude` on PATH, or Ollama for --backend local)
.venv/bin/python -m playloop.loop --max-turns 10
```

Wire the server into an MCP client. `command` must be the **absolute** path to your checkout — MCP
client config is plain JSON and does not expand `~` or environment variables:

```json
{ "mcpServers": { "civrealm": {
    "command": "/path/to/civrealm-mcp-server/.venv/bin/civrealm-mcp-server" } } }
```

### Tests

The offline suites need no CivRealm, no numpy, no network, no Docker, no model and no API key —
so from a clean clone, two commands and nothing else:

```bash
pip install --no-deps -e . && pip install pytest
pytest                                          # 15 tests
```

That is exactly what CI runs (`.github/workflows/tests.yml`, Python 3.10 and 3.11). Each suite is
still runnable on its own:

```bash
.venv/bin/python -m playloop.test_winrule       # win rule / tiebreak
.venv/bin/python -m playloop.test_retry         # retry feedback, schema shape, CLI schema wiring
.venv/bin/python -m playloop.test_orchestrate   # pure orchestration helpers
.venv/bin/python -m playloop.test_ollama_think  # the 4B empty-content regression
```

`scripts/acceptance_check.py` is **not** in that set and pytest does not collect it: it needs the
Docker server, takes real turns, and asserts nothing — it prints the state at each step for a
human to read.

## Project layout

```
src/civrealm_mcp/
  core.py                  # shared env-access layer (CivRealmGame); used by BOTH halves
  server.py                # the MCP server: 7 tools, stdio, worker thread
playloop/                  # the harness — most of the code lives here
  representation.py        # raw obs -> shaped state, per-actor menu, per-turn JSON Schema
  contestant.py            # Claude CLI | Anthropic API | Ollama local | seeded random fixture
  loop.py                  # single-LLM driver: apply, count, retry, write metrics
  winrule.py               # score-at-cap win rule + pure decide_winner
  orchestrate.py           # two contestants, one shared game (random-vs-random verified only)
  test_winrule.py test_retry.py test_orchestrate.py test_ollama_think.py   # offline; `pytest`
  runs/                    # per-run metrics.jsonl / transcript.json / summary.json
scripts/acceptance_check.py # MCP client: drives a whole game purely through the 7 tools (Docker)
.github/workflows/tests.yml # CI: the offline suites on 3.10 + 3.11
SCHEMA.md                  # observation / action / lifecycle schema
REPRESENTATION.md          # the shaping layer and its tradeoffs
SETUP.md                   # exact setup that worked, incl. fixes
STATUS.md                  # chronological log: what was run, what it produced, what broke
EXAMPLE_SHAPED_STATE.txt   # a real shaped state
EXAMPLE_CONSTRAINED_MENU.txt # a real per-actor numbered menu
```

## Status

Verified on macOS arm64: the server starts and a game is driven
start→observe→legal-actions→take-action→end-turn→status→close purely through MCP tools; the play
loop completes 50-turn capped games without crashing on both a paid and a local contestant. Every
*run* claim above traces to a committed run directory. The two exceptions are stated where they
appear and are not run claims: `--backend api` has no committed run (it is verified turn-by-turn
against a committed menu), and the backend-enforcement table records single-turn probes of the
`claude` CLI and the Messages API, not games. The chronological record — including the failures,
the projections that were never re-run live, and the open gaps — is in **[STATUS.md](STATUS.md)**.

## License

This repository's own code is **MIT** ([LICENSE](LICENSE)) — everything here is first-party; no
third-party source is vendored and no Freeciv game logic is reimplemented. Two obligations come
from what it sits on top of, and both are spelled out in **[NOTICE](NOTICE)**:

| component | license | how it is used | what it requires |
|---|---|---|---|
| [CivRealm](https://github.com/bigai-ai/civrealm) | GPL-3.0 | hard dependency, imported **in-process** by `src/civrealm_mcp/core.py` | MIT is GPL-compatible, so the combination is fine to make. But redistributing this code *together with* CivRealm — a container image, a bundle, a vendored wheel — makes a combined work that must go out under **GPL-3.0**, with corresponding source. Shipping this repo alone, naming CivRealm as a dependency, does not. |
| [Freeciv](https://github.com/freeciv/freeciv) | GPL-2.0-or-later | ruleset helptext appears **verbatim inside six committed transcripts** (see below) | those transcript files carry GPL-2.0-or-later text |
| [freeciv-web](https://github.com/freeciv/freeciv-web) | AGPL-3.0 | stock upstream Docker image, run as a **separate process** over the network | nothing for this repo; AGPL §13 falls on whoever operates a *modified* server |

Running any of this privately is not distribution and triggers none of the above.

### Transcript `raw_snapshot`

`playloop/loop.py` records one `raw_snapshot(game)` on a run's first turn (every later turn stores
`null`), so raw→shaped decisions stay auditable. **Every** committed transcript carries one — 11 of
them as of this commit — totalling **2.57 MB of the 9.39 MB** of tracked transcript data (27%).
27.5 KB of each snapshot, across exactly 115 `helptext` fields, is verbatim Freeciv prose. Nothing reads the field at runtime; it exists
for debugging the representation layer. Dropping it would shrink the transcripts by ~27% and remove
the GPL-2.0 text from the tree, at the cost of the raw→shaped audit trail. It is kept deliberately —
see [NOTICE](NOTICE) §2.
