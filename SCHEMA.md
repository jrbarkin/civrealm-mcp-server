# CivRealm Environment Schema

This document records the **observation structure**, **action format**, **turn mechanics**, and
**game-end signalling** of the CivRealm Gymnasium environment, as verified against the live source at
`/Users/jbarkin/Repos/civrealm` (package `civrealm==0.1.2`) and against real data: the captured
observation pickle in `civrealm-llm-baseline/observations_info.txt` (unpickled and introspected) and
a live single-player run (`test_civrealm`) against the Dockerized freeciv-web server.

Citations use `file:line` relative to `civrealm/src/civrealm/...`.

---

## 1. Environment creation

CivRealm registers five Gymnasium env ids on `import civrealm` (`__init__.py:19-38`):

| id | entry point | notes |
|----|-------------|-------|
| `civrealm/FreecivBase-v0` | `FreecivBaseEnv` | **the base env — what we wrap in the MCP server** |
| `civrealm/FreecivTensor-v0` | `FreecivTensorEnv` | tensor (RL) wrapper |
| `civrealm/FreecivTensorMinitask-v0` | `FreecivTensorMinitaskEnv` | tensor minitask |
| `civrealm/FreecivLLM-v0` | `FreecivLLMEnv` | LLM wrapper (adds `info['llm_info']`) |
| `civrealm/FreecivMinitask-v0` | `FreecivMinitaskEnv` | scripted mini-games |

Canonical creation (`random_game.py:26-36`):

```python
import civrealm                 # registers the env ids (side-effect import)
import gymnasium
env = gymnasium.make('civrealm/FreecivBase-v0')
observation, info = env.reset(client_port=6001)          # 2-tuple, observation first
observation, reward, terminated, truncated, info = env.step(action)   # 5-tuple
```

- `reset(seed=None, options=None, client_port=None, **kwargs)` → `(observation, info)` (`freeciv_base_env.py:171-199`).
  If `client_port is None`, a port is auto-picked via `Ports.get()`. `reset()` opens a WebSocket to the
  freeciv server (`ws://<host>:8080/civsocket/<1000+client_port>`), logs in, starts/joins the game, and
  returns the first observation.
- `step(action)` → `(observation, reward, terminated, truncated, info)` (`freeciv_base_env.py:131-160`).
- `close()` disconnects (`freeciv_base_env.py:250-253`).

The env owns a `CivController` (`civ_controller.py`) which holds 11 sub-controllers in `controller_list`
(`civ_controller.py:185-195`). The observation top-level keys are exactly those controller keys.

`FreecivLLMEnv` = `FreecivLLMEnv(Wrapper) → LLMWrapper(Wrapper) → FreecivBaseEnv`. **`LLMWrapper` does not
change the observation** — it only adds `info['llm_info']` and `info['my_player_id']`
(`llm_wrapper.py:84-105`).

---

## 2. Observation structure

`observation` is a **`dict`** with these 11 top-level keys (`TurnManager.get_observation()`
`turn_manager.py:128-141`; each value is the corresponding controller's prop-state):

```
game, rules, map, player, city, tech, unit, options, dipl, gov, client
```

Shapes below are from the **real unpickled** `observations_info.txt` (a 4-player classic game, map 78×52):

```
observation
├─ game     : dict (~20 keys)   — calendar/year labels, name, ruleset flags, scenario info
├─ rules    : dict (~132 keys)  — full game ruleset/settings (aifill, citymindist, foodbox, …)
├─ map      : dict of numpy arrays (the spatial state):
│     status      : ndarray (X, Y)        uint16   # 0 unknown / 1 fogged / 2 seen
│     terrain     : ndarray (X, Y)        uint16
│     extras      : ndarray (X, Y, 34)    bool     # roads, rivers, resources, huts, …
│     output      : ndarray (X, Y, 6)     uint16   # food/shield/trade/… per tile
│     tile_owner  : ndarray (X, Y)        uint16
│     city_owner  : ndarray (X, Y)        uint16
│     unit        : ndarray (X, Y, 52)    uint16   # unit-type counts per tile
│     unit_owner  : ndarray (X, Y)        uint16
├─ player   : dict {player_id -> {...~115 keys...}}  # see below
├─ city     : dict {city_id   -> {...}}              # empty {} when you have no cities yet
├─ tech     : dict {tech_id   -> {...}}              # tech tree state
├─ unit     : dict {unit_id   -> {...~23 keys...}}   # see below
├─ options  : dict (empty by default)
├─ dipl     : dict {player_id -> {diplomatic_state, diplomacy_clause_map, meeting_initializers}}
├─ gov      : dict {name, id, helptext}
└─ client   : dict (empty by default)
```

`map.X`/`map.Y` are the map dimensions for the current game (78×52 in the captured sample; they vary by
map settings). Only `map, player, unit, city, dipl` have rich Gymnasium `observation_space` definitions;
the rest get `Discrete(1)` placeholders but their **state dict is still present** in the observation
(`turn_manager.py:111-126`).

### Per-entity sub-structures (from `*_state.py`)

- **`player[player_id]`** (`player_state.py:26-76`): common fields visible for every player —
  `player_id, name, score, team, is_alive, nation, turns_alive, government, target_government,
  government_name, researching, research_name, tax, science, luxury`; **own-player only** —
  `gold, culture, mood, revolution_finishes, science_cost, bulbs_researched, researching_cost,
  tech_goal, tech_upkeep, techs_researched, total_bulbs_prod, embassy_txt`; for other players a `love`
  field; plus per-tech keys `tech_{tech_id}`.
- **`unit[unit_id]`** (`unit_state.py:53-87`): `owner, health, veteran, x, y`, type descriptors
  `type_rule_name, type_attack_strength, type_defense_strength, type_firepower, type_build_cost,
  type_move_rate, type_vision_radius_sq, type_can_transport, …`; **own-unit only** —
  `home_city, moves_left, upkeep_food, upkeep_shield, upkeep_gold` (set to `-1` for foreign units).
- **`city[city_id]`** (`city_state.py:49-133`): `name, id, owner, size, x, y`; **own-city only** —
  `food_stock, granary_size, shield_stock, buy_cost, prod_food/shield/trade/gold,
  surplus_food/shield/trade/gold, luxury, science, bulbs, can_build_unit (bool array),
  improvements (bool array), turns_to_prod_complete, ppl_happy/content/unhappy/angry, …`
  (foreign-city fields are `-1`).
- **`tech[tech_id]`** (`tech_state.py:39-84`): `name, sup_units, sup_improvements, is_researching,
  is_tech_goal, inv_state (TECH_KNOWN/UNKNOWN/PREREQS_KNOWN), is_req_for_goal, reqs`.
- **`dipl[player_id]`** (`diplomacy_state_ctrl.py:41-57`): `diplomatic_state` (int, `-1` if unknown),
  `diplomacy_clause_map` (list), `meeting_initializers` (int).

### Empty-observation cases (consumers MUST handle)

`observation` is `{}` when the game is over / your player is defeated (`civ_controller.py:430-435`), and
`step()` returns `observation={}, info={}` on a server/begin-turn timeout (`freeciv_base_env.py:142-148`).

---

## 3. Info structure

`info` (built by `civ_controller._get_info()` `civ_controller.py:402-426`):

```python
info = {
    'turn': int,                       # current turn (server counts from 1)
    'mini_game_messages': list,        # minitask messages (usually [] in full games)
    'available_actions': {...},        # legal actions this step — see §4
    # 'image_data': ...                # only if debug.get_webpage_image is set
}
```

`FreecivLLMEnv`/`LLMWrapper` additionally inject `info['llm_info']` (human-readable per-actor view) and
`info['my_player_id']` (`llm_wrapper.py:91-105`). **The base `FreecivBase-v0` env does NOT add these.**

`info['llm_info']` (LLM env only) shape: `{ctrl_type: {actor_id: {'name': str,
'available_actions': [readable strings], 'observations': {'minimap': {...}, 'upper_map': {...}}}}}`.

---

## 4. Action format & enumerating legal actions

An **action is a 3-tuple** `(ctrl_type, actor_id, action_name)`, unpacked verbatim in
`turn_manager.perform_action` (`turn_manager.py:170-174`). It is **not** a dict or a gym-space sample.

Two special actions are handled in `civ_controller.perform_action` (`civ_controller.py:382-388`):

| action passed to `step()` | effect |
|---|---|
| `(ctrl_type, actor_id, action_name)` | perform that action for that actor |
| `None` | **end the current turn** (`send_end_turn()`) |
| `'pass'` | no-op debug message (no game effect) |

`ctrl_type` ∈ `{unit, city, player, dipl, tech, gov}` (the other controllers expose no real actions).
`actor_id` depends on `ctrl_type`: integer `unit_id`/`city_id` for unit/city; the string `'cur_player'`
for tech; `player_id` for gov; the counterpart `player_id` for dipl.
`action_name` is the `Action.action_key` string, e.g. `build_city, fortify, sentry, build_road,
goto_0`…`goto_7` (one per of 8 directions), `produce, city_work, city_buy_production, research_tech,
set_tech_goal, change_gov, increase_sci, start_negotiation`.

### Legal actions are in `info['available_actions']`, NOT in the observation

Shape (`base_action.ActionList.get_action_info()` `base_action.py:237-245`):

```
info['available_actions'] = { ctrl_type: { actor_id: { action_name: <bool/prob/None> } } }
```

Real example (from the captured sample):

```python
info['available_actions'] = {
  'player': {0: {'increase_lux': True, 'decrease_sci': True, ...}, 1: {'start_negotiation_1': True, ...}},
  'tech':   {'cur_player': {'research_tech_Alphabet_2': True, 'set_tech_goal_Astronomy_4': True, ...}},
  'unit':   {103: {'build_city': ..., 'fortify': ..., 'goto_0': ..., 'mine': ..., ...}, 113: {...}},
  'gov':    {0: {'change_gov_Monarchy': True, 'change_gov_Republic': True, ...}},
}
```

Important properties:
- **Only actors that can act appear** — units with 0 moves left / `keep_activity` / under server AI control
  are omitted (`unit_actions.py:324-331`). Don't assume every unit in the observation is listed.
- A value that is falsy means that action is currently invalid; the canonical iteration
  (`base_agent.get_next_valid_actor`, `base_agent.py:51-68`) deletes falsy entries.
- `available_actions` is **`{}`** when your player is defeated or the game is over
  (`civ_controller.py:411-413`).
- It is rebuilt fresh each `step()`; an action tuple captured in a previous turn is invalid after the turn
  ends (per-turn state is torn down in `turn_manager.end_turn()` `turn_manager.py:187-197`).

---

## 5. Turn lifecycle

- **Turn number**: `info['turn']` = `TurnManager._turn`, initialized to 1 (`turn_manager.py:32-33`),
  incremented in `handle_end_turn` (`civ_controller.py:984`). Exposed via `env.unwrapped.civ_controller.get_turn()`.
- **Ending your turn**: call `env.step(None)` → `send_end_turn()` sends a `player_phase_done` packet and
  resets per-turn state (`civ_controller.py:454-469`). In a multi-player game the turn advances only once
  **all** players have ended their phase.
- A single `step()` performs **one** actor action (or ends the turn). A full turn is therefore many
  `step()` calls: act each unit/city/etc., then `step(None)`.

---

## 6. Reward, termination, game end, winner

- **`reward`** = per-step delta of your in-game score: `current_score - last_score`, where
  `current_score = my_player['score']` (`civ_controller.py:450-452`, `turn_manager.py:181-185`). It is `0`
  on most steps and changes between turns (observed: `Reward: 1` on the step that began a new turn after the
  score rose).
- **`terminated`** = `game_has_terminated()` = `game_is_over or game_is_end or my_player_is_defeated()`
  (`civ_controller.py:480-490`). `my_player_is_defeated()` = no cities and no Settlers, or `is_alive == False`
  (`civ_controller.py:303-326`). Victory-condition termination for *your* player is noted as not implemented
  (`# FIXME: check victory conditions.`).
- **`truncated`** = `game_has_truncated()` = `turn > fc_args['max_turns']` (default 1000)
  (`civ_controller.py:471-478`). Observed: `Truncated: True` at turn 3 with `--max_turns=2`.
- **Game-over flags** `game_is_over`/`game_is_end` are set from server chat events (`E_GAME_END`,
  `E_DESTROYED`, "game is over"/"game ended" text) (`civ_controller.py:862-879`).
- **Winner & final per-player score** come from the `PACKET_ENDGAME_PLAYER` (pid 223) handler
  `GameCtrl.handle_endgame_player` (`game/game_ctrl.py:136-143`), stored in `game_ctrl.game_results` and
  exposed via `env.unwrapped.get_game_results()`:

  Real output from the live single-player run (4 players):
  ```python
  {0: {'winner': False, 'score': 1, 'category_score': [1000, 2, 5, 1, 0, 250, 0, 1, 30000, 4000, ...]},
   1: {'winner': False, 'score': 0, 'category_score': [...]},
   2: {'winner': False, 'score': 2, 'category_score': [...]},
   3: {'winner': False, 'score': 1, 'category_score': [...]}}
  ```
  `winner` is a bool per player; `score` is the final score; `category_score` is the per-category vector.
- Per-turn time-series scores across 16 evaluation tags are available via `env.unwrapped.evaluate_game()`
  / `get_final_score()` (`freeciv_base_env.py:205-243`, `eval_tags.py`).

---

## 7. Quick reference for the MCP server

| MCP tool | CivRealm call |
|---|---|
| `start_game` | `gymnasium.make('civrealm/FreecivBase-v0')`; `env.reset(client_port=...)` → `(obs, info)` |
| `get_observation` | last `obs` (dict, §2) |
| `get_legal_actions` | last `info['available_actions']` (§4) |
| `take_action` | `env.step((ctrl_type, actor_id, action_name))` → 5-tuple |
| `end_turn` | `env.step(None)` |
| `get_status` | `info['turn']`, last `reward`, `terminated`, `truncated`, `get_game_results()` winner/scores |
| `close_game` | `env.close()` |

Note on JSON serialization: `map` values are numpy arrays and several leaf values are numpy scalars /
`BitVector`; the MCP server must convert these to JSON-native types before returning (see the server's
`_jsonable()`).
