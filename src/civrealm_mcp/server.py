"""
Minimal MCP server over a single CivRealm (Freeciv) Gymnasium environment.

It owns exactly ONE `FreecivBaseEnv` instance and exposes thin pass-through tools so an
external MCP client (the decision-maker) can drive one game by hand:

    start_game, get_observation, get_legal_actions, take_action, end_turn,
    get_status, close_game

Design notes (verified against civrealm==0.1.2 source, see ../../SCHEMA.md):
- An action is the 3-tuple ``(ctrl_type, actor_id, action_name)``. ``None`` ends the turn.
- Legal actions live in ``info['available_actions']`` (NOT in the observation).
- ``reset()`` -> ``(obs, info)``; ``step(action)`` -> ``(obs, reward, terminated, truncated, info)``.
- There is NO LLM in this server; it is a model-agnostic tool surface.

Two implementation subtleties (both verified by running it):
- stdio safety: CivRealm/gymnasium write to stdout via ``print()``/warnings; the MCP stdio
  transport uses stdout for JSON-RPC. We route all non-protocol output to stderr and only
  hand the real stdout to the transport (see ``main``).
- event loop: CivRealm drives a tornado IOLoop (``IOLoop.start()``) inside reset()/step().
  FastMCP runs tools in the asyncio event loop, so we offload every env call to a single
  dedicated worker thread (consistent IOLoop thread-affinity, no running asyncio loop there).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import sys
from typing import Any

# --- stdout protection (MUST happen before importing civrealm) ----------------
# Keep a handle to the real stdout for the JSON-RPC transport; send everything
# else (civrealm print()s, gym warnings) to stderr so it can't corrupt the stream.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s [civrealm-mcp] %(message)s",
)
log = logging.getLogger("civrealm_mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("civrealm")

# All CivRealm/env work runs on this single thread so tornado's IOLoop has a stable
# thread with no already-running asyncio loop.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="civrealm-env")


async def _offload(fn, *args, **kwargs):
    """Run a blocking env operation on the dedicated worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(fn, *args, **kwargs))


# --- single-environment state -------------------------------------------------
_env: Any = None
_state: dict[str, Any] = {
    "running": False,
    "obs": None,
    "info": None,
    "reward": None,
    "terminated": None,
    "truncated": None,
    "step_count": 0,
    "client_port": None,
    "max_turns": None,
    "username": None,
}


# --- JSON serialization for numpy / BitVector / sets --------------------------
def _jsonable(x: Any) -> Any:
    """Recursively convert civrealm observation/info values into JSON-native types."""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    try:
        import numpy as np

        if isinstance(x, np.ndarray):
            return x.tolist()
        if isinstance(x, np.generic):
            return x.item()
    except Exception:  # pragma: no cover - numpy is a civrealm dependency
        pass
    try:
        from BitVector import BitVector

        if isinstance(x, BitVector):
            return x.get_bitvector_in_ascii()
    except Exception:  # pragma: no cover
        pass
    if isinstance(x, dict):
        # JSON object keys must be strings; civrealm keys ints (player/unit/city ids).
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (set, frozenset)):
        return [_jsonable(v) for v in sorted(x, key=str)]
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def _coerce_actor_id(actor_id: int | str) -> int | str:
    """unit/city/player/gov/dipl ids are ints; tech uses the string 'cur_player'."""
    if isinstance(actor_id, int):
        return actor_id
    s = str(actor_id)
    if s.lstrip("-").isdigit():
        return int(s)
    return s


def _players_summary(obs: Any) -> dict[str, Any]:
    """Compact {player_id: {name, score, is_alive}} from the last observation, if any."""
    out: dict[str, Any] = {}
    if isinstance(obs, dict):
        players = obs.get("player") or {}
        if isinstance(players, dict):
            for pid, p in players.items():
                if isinstance(p, dict):
                    out[str(pid)] = {
                        "name": _jsonable(p.get("name")),
                        "score": _jsonable(p.get("score")),
                        "is_alive": _jsonable(p.get("is_alive")),
                    }
    return out


def _status_dict() -> dict[str, Any]:
    """Shared status payload used by several tools."""
    status: dict[str, Any] = {
        "running": _state["running"],
        "turn": _jsonable(_state["info"].get("turn")) if isinstance(_state["info"], dict) else None,
        "step_count": _state["step_count"],
        "last_reward": _jsonable(_state["reward"]),
        "terminated": _state["terminated"],
        "truncated": _state["truncated"],
        "client_port": _state["client_port"],
        "max_turns": _state["max_turns"],
        "username": _state["username"],
    }
    if _env is not None:
        try:
            status["my_player_id"] = _jsonable(_env.get_playerid())
        except Exception:
            status["my_player_id"] = None
    status["players"] = _players_summary(_state["obs"])
    # Winner / final scores are only populated once the server sends the endgame packet.
    if _env is not None and (_state["terminated"] or _state["truncated"]):
        try:
            results = _env.get_game_results()
            if results:
                status["game_results"] = _jsonable(results)
                winners = [str(pid) for pid, r in results.items()
                           if isinstance(r, dict) and r.get("winner")]
                status["winner_player_ids"] = winners
        except Exception as e:  # pragma: no cover
            status["game_results_error"] = repr(e)
    return status


# ============================ ENV OPERATIONS (worker thread) ==================
def _impl_start_game(client_port: int, max_turns: int, username: str) -> dict:
    global _env
    # Lazy import so the MCP server starts instantly and only loads civrealm on demand.
    from civrealm.configs import fc_args
    from civrealm.envs.freeciv_base_env import FreecivBaseEnv

    if _env is not None:
        try:
            _env.close()
        except Exception as e:  # pragma: no cover
            log.warning("Error closing previous env: %r", e)
        _env = None

    # Configure for single-player vs built-in AI. NOTE: we keep CivRealm's default
    # multiplayer_game/aifill settings (the same config test_civrealm uses for its
    # "single player vs built-in AIs" mode); minp=1 starts the game immediately.
    fc_args["username"] = username
    fc_args["max_turns"] = max_turns
    fc_args["minp"] = 1
    # Give the (emulated, amd64-on-arm64) server headroom to send the first begin_turn.
    fc_args["begin_turn_timeout"] = max(fc_args.get("begin_turn_timeout", 30), 120)

    log.info("start_game: FreecivBaseEnv(username=%s) port=%s max_turns=%s",
             username, client_port, max_turns)
    _env = FreecivBaseEnv(username=username)
    obs, info = _env.reset(client_port=client_port)

    _state.update(
        running=True, obs=obs, info=info, reward=0,
        terminated=False, truncated=False, step_count=0,
        client_port=client_port, max_turns=max_turns, username=username,
    )

    status = _status_dict()
    avail = info.get("available_actions", {}) if isinstance(info, dict) else {}
    status["message"] = "game started"
    status["legal_action_actor_types"] = list(avail.keys())
    status["num_units"] = len(obs.get("unit", {})) if isinstance(obs, dict) else 0
    status["observation_keys"] = list(obs.keys()) if isinstance(obs, dict) else []
    return status


def _impl_take_action(ctrl_type: str, actor_id: int | str, action_name: str) -> dict:
    aid = _coerce_actor_id(actor_id)
    info = _state["info"]
    avail = info.get("available_actions", {}) if isinstance(info, dict) else {}
    ctrl_actions = avail.get(ctrl_type, {})
    actor_actions = ctrl_actions.get(aid, {}) if isinstance(ctrl_actions, dict) else {}
    if not actor_actions or action_name not in actor_actions:
        return {
            "error": "illegal or unavailable action",
            "ctrl_type": ctrl_type,
            "actor_id": str(actor_id),
            "action_name": action_name,
            "valid_actions_for_actor": [k for k, v in _jsonable(actor_actions).items() if v]
            if actor_actions else [],
            "hint": "call get_legal_actions to see currently-valid actions",
        }
    obs, reward, terminated, truncated, new_info = _env.step((ctrl_type, aid, action_name))
    _state.update(obs=obs, info=new_info, reward=reward,
                  terminated=bool(terminated), truncated=bool(truncated),
                  step_count=_state["step_count"] + 1)
    status = _status_dict()
    status["action_taken"] = [ctrl_type, _jsonable(aid), action_name]
    return status


def _impl_end_turn() -> dict:
    obs, reward, terminated, truncated, new_info = _env.step(None)
    _state.update(obs=obs, info=new_info, reward=reward,
                  terminated=bool(terminated), truncated=bool(truncated),
                  step_count=_state["step_count"] + 1)
    status = _status_dict()
    status["action_taken"] = None
    status["message"] = "turn ended"
    return status


def _impl_close_game() -> dict:
    global _env
    try:
        _env.close()
        msg = "game closed"
    except Exception as e:  # pragma: no cover
        msg = f"closed with error: {e!r}"
    _env = None
    _state.update(running=False, obs=None, info=None, reward=None,
                  terminated=None, truncated=None, step_count=0)
    return {"running": False, "message": msg}


# ================================ MCP TOOLS ===================================
@mcp.tool()
async def start_game(client_port: int = 6001, max_turns: int = 1000, username: str = "myagent") -> dict:
    """Start a new single-player CivRealm game against the built-in AIs and return the
    initial status.

    Requires a running freeciv-web Docker server reachable on http://localhost:8080
    (see SETUP.md). Creates/resets a single ``civrealm/FreecivBase-v0`` environment.

    Args:
        client_port: freeciv client port to connect on (default 6001; valid: 6001 or 6300-6331).
        max_turns: turn limit; the game is truncated once turn > max_turns (default 1000).
        username: player name used to log in (no spaces/underscores).
    """
    return await _offload(_impl_start_game, client_port, max_turns, username)


@mcp.tool()
async def get_observation(keys: list[str] | None = None) -> dict:
    """Return the current observation as JSON.

    The observation is a dict with top-level keys:
    game, rules, map, player, city, tech, unit, options, dipl, gov, client (see SCHEMA.md).
    The 'map' value contains large arrays; pass ``keys`` to fetch only some top-level
    sections, e.g. keys=["unit","player","city"], to avoid huge payloads.

    Args:
        keys: optional list of top-level observation keys to include (default: all).
    """
    if _env is None or not _state["running"]:
        return {"error": "no game in progress; call start_game first"}
    obs = _state["obs"]
    if not isinstance(obs, dict):
        return {"observation": _jsonable(obs),
                "note": "observation is empty (game over or player defeated)"}
    if keys:
        selected = {k: obs[k] for k in keys if k in obs}
        missing = [k for k in keys if k not in obs]
        result = {"observation": _jsonable(selected)}
        if missing:
            result["missing_keys"] = missing
        return result
    return {"observation": _jsonable(obs), "observation_keys": list(obs.keys())}


@mcp.tool()
async def get_legal_actions(ctrl_type: str | None = None, actor_id: int | str | None = None) -> dict:
    """Return the legal actions available this step, as {ctrl_type: {actor_id: {action_name: validity}}}.

    These come from ``info['available_actions']``. Only actors that can currently act are
    listed (e.g. units with 0 moves left are omitted). Pass an action you find here to
    ``take_action``. An empty result means no actor can act (so call ``end_turn``).

    Args:
        ctrl_type: optionally restrict to one of unit, city, player, dipl, tech, gov.
        actor_id: optionally restrict to a single actor id (use with ctrl_type).
    """
    if _env is None or not _state["running"]:
        return {"error": "no game in progress; call start_game first"}
    info = _state["info"]
    avail = info.get("available_actions", {}) if isinstance(info, dict) else {}
    avail = _jsonable(avail)  # keys -> str, validities -> JSON

    if ctrl_type is not None:
        sub = avail.get(ctrl_type, {})
        if actor_id is not None:
            sub = sub.get(str(actor_id), {}) if isinstance(sub, dict) else {}
            return {"ctrl_type": ctrl_type, "actor_id": str(actor_id), "available_actions": sub}
        return {"ctrl_type": ctrl_type, "available_actions": sub}

    return {
        "available_actions": avail,
        "actor_types": list(avail.keys()),
        "note": "submit one via take_action(ctrl_type, actor_id, action_name); or end_turn()",
    }


@mcp.tool()
async def take_action(ctrl_type: str, actor_id: int | str, action_name: str) -> dict:
    """Perform ONE legal action for ONE actor: env.step((ctrl_type, actor_id, action_name)).

    The action must be currently legal (check get_legal_actions first). This advances one
    step but does NOT end your turn — call end_turn() when done acting this turn.

    Args:
        ctrl_type: one of unit, city, player, dipl, tech, gov.
        actor_id: the actor id (int for unit/city/player/gov/dipl; 'cur_player' for tech).
        action_name: the action key from get_legal_actions (e.g. 'build_city', 'goto_3').
    """
    if _env is None or not _state["running"]:
        return {"error": "no game in progress; call start_game first"}
    return await _offload(_impl_take_action, ctrl_type, actor_id, action_name)


@mcp.tool()
async def end_turn() -> dict:
    """End the current player's turn: env.step(None).

    In a multi-player game the game turn only advances once all players have ended their
    phase. Returns the updated status (new turn, reward, terminated/truncated).
    """
    if _env is None or not _state["running"]:
        return {"error": "no game in progress; call start_game first"}
    return await _offload(_impl_end_turn)


@mcp.tool()
async def get_status() -> dict:
    """Return current game status: turn number, per-player scores, terminated/truncated
    flags, my_player_id, and (once the game has ended) the winner and final game results.
    """
    if _env is None:
        return {"running": False, "message": "no game in progress; call start_game first"}
    return _status_dict()


@mcp.tool()
async def close_game() -> dict:
    """Close the current game and disconnect the environment (env.close())."""
    if _env is None:
        return {"running": False, "message": "no game to close"}
    return await _offload(_impl_close_game)


# ================================ ENTRY POINT =================================
def main() -> None:
    """Run the server over stdio, keeping JSON-RPC on the real stdout."""
    import anyio
    from mcp.server.stdio import stdio_server

    async def _run() -> None:
        # Restore the real stdout only long enough for stdio_server() to capture the
        # fd-1 buffer for JSON-RPC; then route prints back to stderr for the session.
        sys.stdout = _REAL_STDOUT
        try:
            async with stdio_server() as (read_stream, write_stream):
                sys.stdout = sys.stderr
                await mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
        finally:
            sys.stdout = sys.stderr

    log.info("civrealm-mcp-server starting (stdio). Tools: start_game, get_observation, "
             "get_legal_actions, take_action, end_turn, get_status, close_game")
    anyio.run(_run)


if __name__ == "__main__":
    main()
