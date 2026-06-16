"""
State representation / shaping layer for the single-agent play loop.

Raw CivRealm observations are huge (numpy map arrays, ~115 fields per player, the full tech
tree) and the action keys are cryptic (`goto_3`, `research_tech_Alphabet_2`). The documented
CivRealm failure mode is that models can't control many units from raw local observations.

This layer condenses one (observation, info) pair into:
  - a compact strategic summary (turn, score, gold, government, research, counts),
  - your cities and units with position/stats,
  - and, per actor, the EXACT legal action keys for this turn (annotated readably, e.g.
    `goto_1 [move_North]`), since `info['available_actions']` is where legality lives.

`render(shaped)` produces the text shown to the contestant. `shape()` returns a structured
dict for metrics/debugging. `raw_snapshot()` keeps raw->shaped debuggable (saved per turn).

Design choices (documented in REPRESENTATION.md):
  - We list the EXACT action keys the model must return (not aliases), so a low illegal-action
    rate reflects the model reading our representation, not us translating for it. `goto_*` keys
    get a readable compass annotation in brackets.
  - We do NOT enum-constrain the model's choice, so the illegal-action rate is a real measurement.
  - Per-actor action lists are capped (default 60) with the dropped count surfaced — no silent truncation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from civrealm_mcp.core import GOTO_TO_COMPASS, jsonable

ACTION_CAP = 60  # max legal actions listed per actor; overflow is reported, not hidden
CTRL_ORDER = ["unit", "city", "tech", "gov", "player", "dipl"]


# "Hold/idle a unit" is the #1 source of illegal proposals: the model reaches for `fortify`,
# but the legal hold action depends on unit state (`fortify` vs `keep_activity` vs the unit
# must `cancel_order` first). Annotate whichever hold actions are actually offered so the model
# picks the listed one instead of defaulting to `fortify`.
HOLD_HINTS = {
    "keep_activity": "hold: stay put / keep doing current activity",
    "fortify": "hold: dig in (defensive)",
    "sentry": "hold: wait and watch",
    "cancel_order": "stop this unit's current order",
}


def _annotate(action_key: str) -> str:
    """Add a readable hint to cryptic keys; the model still returns the raw key."""
    if action_key in GOTO_TO_COMPASS:
        return f"{action_key} [{GOTO_TO_COMPASS[action_key]}]"
    if action_key in HOLD_HINTS:
        return f"{action_key} [{HOLD_HINTS[action_key]}]"
    return action_key


@dataclass
class ShapedState:
    turn: int | None
    max_turns: int | None
    my_player_id: Any
    summary: dict
    cities: list[dict]
    units: list[dict]
    actionable: dict  # {ctrl_type: {actor_id(str): [action_key, ...]}}
    dropped_actions: dict = field(default_factory=dict)  # {ctrl_type/actor: dropped_count}

    def num_legal_actions(self) -> int:
        return sum(len(acts) for actors in self.actionable.values() for acts in actors.values())


def shape(game) -> ShapedState:
    """Condense game.obs + game.info into a ShapedState."""
    obs = game.obs if isinstance(game.obs, dict) else {}
    me = game.my_player_id()
    players = obs.get("player") or {}
    my = players.get(me, {}) if isinstance(players, dict) else {}

    summary = {
        "turn": game.turn,
        "max_turns": game.max_turns,
        "player_id": jsonable(me),
        "name": jsonable(my.get("name")),
        "score": jsonable(my.get("score")),
        "gold": jsonable(my.get("gold")),
        "government": jsonable(my.get("government_name")),
        "tax": jsonable(my.get("tax")),
        "science": jsonable(my.get("science")),
        "luxury": jsonable(my.get("luxury")),
        "research_name": jsonable(my.get("research_name")),
        "researching_id": jsonable(my.get("researching")),
        "researching_cost": jsonable(my.get("researching_cost")),
        "bulbs_researched": jsonable(my.get("bulbs_researched")),
        "tech_goal": jsonable(my.get("tech_goal")),
        "techs_researched": jsonable(my.get("techs_researched")),
    }

    # My cities / units (owner == my_player_id).
    cities: list[dict] = []
    for cid, c in (obs.get("city") or {}).items():
        if not isinstance(c, dict) or c.get("owner") != me:
            continue
        producing = c.get("prod_process")
        if not isinstance(producing, str):
            producing = f"kind={jsonable(c.get('production_kind'))} value={jsonable(c.get('production_value'))}"
        cities.append({
            "id": jsonable(cid), "name": jsonable(c.get("name")),
            "x": jsonable(c.get("x")), "y": jsonable(c.get("y")), "size": jsonable(c.get("size")),
            "producing": producing,
            "surplus": {"food": jsonable(c.get("surplus_food")), "shield": jsonable(c.get("surplus_shield")),
                        "trade": jsonable(c.get("surplus_trade"))},
        })

    units: list[dict] = []
    for uid, u in (obs.get("unit") or {}).items():
        if not isinstance(u, dict) or u.get("owner") != me:
            continue
        units.append({
            "id": jsonable(uid), "type": jsonable(u.get("type_rule_name")),
            "x": jsonable(u.get("x")), "y": jsonable(u.get("y")),
            "moves_left": jsonable(u.get("moves_left")), "health": jsonable(u.get("health")),
        })

    # Legal actions per actor (already masked to valid==truthy by civrealm).
    actionable: dict[str, dict[str, list[str]]] = {}
    dropped: dict[str, int] = {}
    avail = game.available_actions
    for ctrl_type, actors in (avail or {}).items():
        if not isinstance(actors, dict):
            continue
        actionable[ctrl_type] = {}
        for actor_id, action_map in actors.items():
            if not isinstance(action_map, dict):
                continue
            valid = [k for k, v in action_map.items() if v]
            if not valid:
                continue
            if len(valid) > ACTION_CAP:
                dropped[f"{ctrl_type}:{actor_id}"] = len(valid) - ACTION_CAP
                valid = valid[:ACTION_CAP]
            actionable[ctrl_type][str(actor_id)] = valid
        if not actionable[ctrl_type]:
            del actionable[ctrl_type]

    summary["num_cities"] = len(cities)
    summary["num_units"] = len(units)
    return ShapedState(turn=game.turn, max_turns=game.max_turns, my_player_id=jsonable(me),
                       summary=summary, cities=cities, units=units,
                       actionable=actionable, dropped_actions=dropped)


def _summary_lines(s: ShapedState) -> list[str]:
    """The strategic-summary header block, shared by render() and render_menu()."""
    sm = s.summary
    return [
        f"=== CivRealm — turn {sm.get('turn')} of {sm.get('max_turns')} ===",
        (f"You are player {sm.get('player_id')} \"{sm.get('name')}\". "
         f"Score {sm.get('score')}. Gold {sm.get('gold')}. Government: {sm.get('government')}. "
         f"Rates tax/sci/lux {sm.get('tax')}/{sm.get('science')}/{sm.get('luxury')}."),
        (f"Researching: {sm.get('research_name')} "
         f"({sm.get('bulbs_researched')}/{sm.get('researching_cost')} bulbs). "
         f"Techs known: {sm.get('techs_researched')}."),
        (f"You have {sm.get('num_cities')} cities and {sm.get('num_units')} units. "
         f"Coordinates are (x=column, y=row); North is up (decreasing y)."),
    ]


def render(s: ShapedState) -> str:
    """Render the shaped state as the compact text shown to the contestant (free-form mode)."""
    lines: list[str] = _summary_lines(s)

    units_by_id = {str(u["id"]): u for u in s.units}
    cities_by_id = {str(c["id"]): c for c in s.cities}

    if s.cities:
        lines.append(f"\nYOUR CITIES ({len(s.cities)}):")
        for c in s.cities:
            su = c["surplus"]
            lines.append(f"- city {c['id']} \"{c['name']}\" at ({c['x']},{c['y']}) size {c['size']}, "
                         f"producing {c['producing']}; surplus food/shield/trade "
                         f"{su['food']}/{su['shield']}/{su['trade']}")
            acts = s.actionable.get("city", {}).get(str(c["id"]))
            if acts:
                lines.append(f"    actions: {', '.join(_annotate(a) for a in acts)}")

    if s.units:
        lines.append(f"\nYOUR UNITS ({len(s.units)}):")
        for u in s.units:
            lines.append(f"- unit {u['id']} {u['type']} at ({u['x']},{u['y']}) moves {u['moves_left']}")
            acts = s.actionable.get("unit", {}).get(str(u["id"]))
            if acts:
                lines.append(f"    actions: {', '.join(_annotate(a) for a in acts)}")

    # Remaining actor types (tech/gov/player/dipl) and any unit/city actors without a stat row.
    for ctrl_type in CTRL_ORDER:
        actors = s.actionable.get(ctrl_type)
        if not actors:
            continue
        if ctrl_type == "unit":
            extra = {a: v for a, v in actors.items() if a not in units_by_id}
        elif ctrl_type == "city":
            extra = {a: v for a, v in actors.items() if a not in cities_by_id}
        else:
            extra = actors
        if not extra:
            continue
        lines.append(f"\n{ctrl_type.upper()} actions:")
        for actor_id, acts in extra.items():
            lines.append(f"- {ctrl_type} {actor_id}: {', '.join(_annotate(a) for a in acts)}")

    if s.dropped_actions:
        drop = ", ".join(f"{k} (+{n} more not shown)" for k, n in s.dropped_actions.items())
        lines.append(f"\n[note: some actors had more than {ACTION_CAP} legal actions; truncated: {drop}]")

    return "\n".join(lines)


def menu_to_schema(menu: ChoiceMenu) -> dict:
    """Per-turn JSON Schema for STRUCTURED-OUTPUT enforcement (Ollama `format=<schema>` /
    grammar-constrained decoding). One integer property per actor, each constrained to an `enum`
    of that actor's valid option numbers (0=skip included); ALL actors required and no extra keys.

    With this, a constrained decoder structurally CANNOT: invent an actor that isn't on the board
    (additionalProperties:false + fixed `required` keys), pick an out-of-range option (enum), or
    omit an actor (required). It turns "validate + retry" into "can't be emitted in the first place".
    """
    props = {actor_key: {"type": "integer", "enum": sorted(opts.keys())}
             for actor_key, opts in menu.index.items()}
    return {
        "type": "object",
        "properties": {
            "plan": {"type": "string"},
            "choices": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
                "additionalProperties": False,
            },
        },
        "required": ["choices"],
        "additionalProperties": False,
    }


def raw_snapshot(game) -> dict:
    """JSON-able raw obs (minus the bulky 'map' arrays) + info, for raw->shaped debugging."""
    obs = game.obs if isinstance(game.obs, dict) else {}
    raw_obs = {k: jsonable(v) for k, v in obs.items() if k != "map"}
    if "map" in obs:
        raw_obs["map"] = "<omitted: large numpy arrays>"
    return {"obs": raw_obs, "info": jsonable(game.info)}


# ============================ Constrained-selection menu ============================
# Instead of free-form action strings, the contestant picks an option NUMBER from a small,
# per-actor menu of that actor's legal actions. Option 0 is always "skip". Because the model
# returns indices into the legal set, an illegal action is impossible by construction. We group
# per actor (not one flat 300-item list) so each decision is over a small, legible option set,
# while still using a single subagent call per turn.

@dataclass
class ChoiceMenu:
    summary_lines: list[str]
    # ordered render rows: (actor_key, header, [(option_int, annotated_action_label)])
    actors: list[tuple[str, str, list[tuple[int, str]]]]
    # actor_key -> {option_int -> (ctrl_type, actor_id_str, action_key)};  option 0 -> None (skip)
    index: dict[str, dict[int, tuple[str, str, str] | None]]
    dropped_actions: dict = field(default_factory=dict)

    def num_actors(self) -> int:
        return len(self.actors)


def build_choice_menu(game) -> ChoiceMenu:
    """Build a per-actor numbered menu over the turn's legal actions, plus an index to resolve
    a chosen (actor_key, option_int) back to a concrete (ctrl_type, actor_id, action_key)."""
    s = shape(game)
    units_by = {str(u["id"]): u for u in s.units}
    cities_by = {str(c["id"]): c for c in s.cities}

    actors: list[tuple[str, str, list[tuple[int, str]]]] = []
    index: dict[str, dict[int, tuple[str, str, str] | None]] = {}

    for ctrl_type in CTRL_ORDER:
        for actor_id, acts in (s.actionable.get(ctrl_type) or {}).items():
            actor_key = f"{ctrl_type}:{actor_id}"
            opt_index: dict[int, tuple[str, str, str] | None] = {0: None}
            rendered: list[tuple[int, str]] = [(0, "skip")]
            for i, a in enumerate(acts, start=1):
                opt_index[i] = (ctrl_type, actor_id, a)
                rendered.append((i, _annotate(a)))
            index[actor_key] = opt_index

            if ctrl_type == "unit" and actor_id in units_by:
                u = units_by[actor_id]
                header = f"{actor_key}  {u['type']} at ({u['x']},{u['y']}) moves {u['moves_left']}"
            elif ctrl_type == "city" and actor_id in cities_by:
                c = cities_by[actor_id]
                header = (f"{actor_key}  \"{c['name']}\" at ({c['x']},{c['y']}) size {c['size']} "
                          f"producing {c['producing']}")
            else:
                header = actor_key
            actors.append((actor_key, header, rendered))

    return ChoiceMenu(summary_lines=_summary_lines(s), actors=actors, index=index,
                      dropped_actions=s.dropped_actions)


def render_menu(menu: ChoiceMenu) -> str:
    """Render the numbered per-actor choice menu shown to the contestant (constrained mode)."""
    lines = list(menu.summary_lines)
    lines.append("\nChoose ONE option NUMBER for each actor below (0 = skip). You may only pick "
                 "numbers shown for that actor — so every choice is a legal move.")
    for actor_key, header, opts in menu.actors:
        lines.append("")
        lines.append(header)
        lines.append("   " + "   ".join(f"{n}) {label}" for n, label in opts))
    if menu.dropped_actions:
        drop = ", ".join(f"{k} (+{n} more not shown)" for k, n in menu.dropped_actions.items())
        lines.append(f"\n[note: some actors had more than {ACTION_CAP} legal actions; truncated: {drop}]")
    return "\n".join(lines)
