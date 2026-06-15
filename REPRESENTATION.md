# State Representation (shaping layer)

The single-agent play loop does **not** feed raw CivRealm observations to the model. Raw
observations are huge and cryptic, and the documented CivRealm failure mode is that *models
can't control many units from raw local observations*. `playloop/representation.py` is the
explicit shaping layer that turns one `(observation, info)` pair into compact, readable text
the model can reason over, plus the exact legal actions for the turn.

Source of truth for the underlying fields: [SCHEMA.md](SCHEMA.md).

## What the raw input looks like (and why it can't go to the model directly)

- `observation` is a dict of 11 sections; `observation['map']` alone is several numpy arrays
  (e.g. a `(78, 52, 52)` unit array ≈ 200k ints).
- `observation['player']` has ~115 fields per player × 4 players; `observation['tech']` is the
  whole tech tree.
- Actions are cryptic keys: `goto_3`, `research_tech_Alphabet_2`, `change_gov_Monarchy`.
- Legality is **not** in the observation — it lives in `info['available_actions']` as
  `{ctrl_type: {actor_id: {action_key: valid_bool}}}`.

Dumping that verbatim blows the context window and buries the few facts a turn decision needs.

## What the shaped state contains

`shape(game) → ShapedState`, then `render(state) → str`. The rendered text has four parts:

1. **Strategic summary** (one block): turn / cap, player id + name, score, gold, government,
   tax/sci/lux rates, current research (name + bulbs progress), techs known, and counts of your
   cities and units. This is what the model needs to steer (expand? research? grow?).
2. **Your cities**: for each city you own — id, name, `(x,y)`, size, what it's producing,
   and food/shield/trade surplus, followed by that city's legal actions.
3. **Your units**: for each unit you own — id, type (e.g. `Settlers`), `(x,y)`, moves left,
   followed by that unit's legal actions.
4. **Other actionable actors** (`tech`, `gov`, `player`, `dipl`): their legal actions.

Only **your** actors are listed (filtered by `owner == my_player_id`). Enemy units/cities, the
full map arrays, the tech tree, and per-tile terrain are intentionally omitted — see Tradeoffs.

## Key design decisions

- **We list the EXACT action keys the model must return**, annotating only the cryptic ones.
  Movement keys get a readable compass hint in brackets using CivRealm's own mapping
  (`configs/llm_wrapper_settings.yaml`): `goto_1 [move_North]`, `goto_4 [move_East]`, etc. The
  model is told to return the token *before* the bracket (`goto_1`). This keeps the
  illegal-action rate a measurement of *the model reading our representation*, not of us
  translating its free-text back into keys.
- **We do NOT enum-constrain the choice.** The contestant returns free-form action strings; the
  loop checks each against `info['available_actions']`. If we hard-constrained to an enum the
  illegal rate would be 0 by construction and meaningless. (As a small robustness courtesy the
  loop will still map an obvious alias back to its key — e.g. `move_North` → `goto_1`, or a
  spacing variant — and counts it legal; this is logged per move in the transcript.)
- **Legality is surfaced fresh every turn.** `available_actions` is rebuilt each step, and only
  actors that can currently act appear — so the model never sees a unit with 0 moves as actionable.
- **Per-actor action lists are capped at `ACTION_CAP = 60`**, and any overflow is reported in
  the text (`[note: ... +N more not shown]`) and in the shaped state's `dropped_actions` — no
  silent truncation. In practice only `tech` (research + goal for the whole tree) approaches this.

## The per-turn inner loop (playloop/loop.py)

A turn is many actions then `end_turn`. Each turn:

1. `shape()` + `render()` the current state.
2. Ask the contestant (`claude` subagent) for a JSON batch of moves
   `[{ctrl_type, actor_id, action}, ...]` — one decision per actor it wants to act; omit to skip.
3. Apply them in order, **re-validating each against the current `available_actions`** (acting
   changes the env). Each move is recorded as `applied` / `illegal` / `duplicate_actor` /
   `malformed` / `env_error`.
4. `end_turn()` to advance.

A batch (whole-turn) decision — rather than one subagent call per actor — is both the harder,
more honest test of the representation (can the model command *all* its units coherently at
once, the documented failure case?) and far cheaper (one subagent call per turn).

## Raw → shaped is debuggable

`raw_snapshot(game)` returns the JSON-able raw `obs` (with the bulky `map` arrays elided) and
`info`. The loop saves the full raw snapshot for the **first turn** of every run in
`transcript.json` alongside that turn's shaped text, so you can diff raw vs shaped. Every turn's
shaped text, the model's raw output, and per-move outcomes are saved for all turns.

## Tradeoffs / known gaps

- **No spatial/terrain context beyond position + legal actions.** We rely on the legal-action
  mask to encode "what's possible here" (e.g. `build_city` legal ⇒ buildable tile; `irrigation`
  legal ⇒ irrigable) rather than decoding `map.terrain` ids into names (which needs the ruleset).
  This keeps the prompt small but means the model can't see *unexplored* directions or nearby
  enemies except through which `goto_*`/attack actions are offered. A richer local minimap (like
  civrealm's own `llm_info`) is the obvious next iteration if coherence needs it.
- **No cross-turn memory.** Each turn is a fresh subagent call with the full current state. Good
  for isolation and cost; the model can't remember a multi-turn plan beyond what the board shows.
- **Tech is verbose.** The research/goal action set is large; it's listed (capped) rather than
  summarized, so the model picks exact keys.

## Example (real shaped state)

See `EXAMPLE_SHAPED_STATE.txt` for a real turn-1 rendering captured from a run.
