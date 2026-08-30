"""
Score-based win rule for CivRealm games capped at a turn limit.

A true Freeciv victory (space race / domination / etc.) takes far more turns than any practical
cap, and the true-game-end path is unverified. So for capped head-to-head games we decide the
winner by **in-game score at the cap**, with a deterministic tiebreak.

- `get_final_result(game)` reads one player's end-of-game metrics from the live state.
- `decide_winner(stateA, stateB)` is PURE (operates on two metric dicts) so it is trivially
  unit-testable, and is reused by the two-agent orchestrator (`playloop/orchestrate.py`).

Metrics come from the observation (see SCHEMA.md): score and techs from the player dict; cities
and population (sum of city sizes) and units from the owned-entity dicts.
"""

from __future__ import annotations

from civrealm_mcp.core import jsonable

# Primary metric, then tiebreak order. All are read from the observation.
DEFAULT_PRIMARY = "score"
DEFAULT_TIEBREAK = ("cities", "techs", "population", "units")


def get_final_result(game) -> dict:
    """End-of-game metrics for game's own player, read from the current observation."""
    obs = game.obs if isinstance(game.obs, dict) else {}
    me = game.my_player_id()
    player = (obs.get("player") or {}).get(me, {}) if isinstance(obs.get("player"), dict) else {}
    cities = [c for c in (obs.get("city") or {}).values()
              if isinstance(c, dict) and c.get("owner") == me]
    units = [u for u in (obs.get("unit") or {}).values()
             if isinstance(u, dict) and u.get("owner") == me]

    def _int(x) -> int:
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    return {
        "player_id": jsonable(me),
        "score": _int(player.get("score")),
        "techs": _int(player.get("techs_researched")),
        "cities": len(cities),
        "population": sum(_int(c.get("size")) for c in cities),
        "units": len(units),
        "turn": game.turn,
    }


def decide_winner(state_a: dict, state_b: dict,
                  primary: str = DEFAULT_PRIMARY,
                  tiebreak: tuple[str, ...] = DEFAULT_TIEBREAK) -> dict:
    """PURE. Compare two result dicts. Higher `primary` wins; ties broken by `tiebreak` order.

    Returns {winner: 'A'|'B'|'tie', by: <metric or None>, scores: {A,B}, reason: str}.
    """
    order = [primary] + [k for k in tiebreak if k != primary]
    for key in order:
        av = state_a.get(key, 0) or 0
        bv = state_b.get(key, 0) or 0
        if av != bv:
            return {
                "winner": "A" if av > bv else "B",
                "by": key,
                "scores": {"A": state_a.get(primary, 0) or 0, "B": state_b.get(primary, 0) or 0},
                "reason": f"{key}: A={av} vs B={bv}",
            }
    return {
        "winner": "tie",
        "by": None,
        "scores": {"A": state_a.get(primary, 0) or 0, "B": state_b.get(primary, 0) or 0},
        "reason": f"equal on all of {order}",
    }
