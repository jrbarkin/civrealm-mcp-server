"""
Minimal MCP server over a single CivRealm (Freeciv) Gymnasium environment.

Owns exactly ONE FreecivBaseEnv (via the shared civrealm_mcp.core.CivRealmGame) and exposes
thin pass-through tools so an external MCP client (the decision-maker) can drive one game:

    start_game, get_observation, get_legal_actions, take_action, end_turn,
    get_status, close_game

There is NO LLM in this server; it is a model-agnostic tool surface. See ../../SCHEMA.md.

Two implementation subtleties (both verified by running it):
- stdio safety: CivRealm/gymnasium write to stdout via print()/warnings; the MCP stdio
  transport uses stdout for JSON-RPC. We route all non-protocol output to stderr and only
  hand the real stdout to the transport (see main()).
- event loop: CivRealm drives a tornado IOLoop inside reset()/step(); FastMCP runs tools in
  the asyncio loop. We offload every env call to a single dedicated worker thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import sys
from typing import Any

# --- stdout protection (MUST happen before importing civrealm) ----------------
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s [civrealm-mcp] %(message)s")
log = logging.getLogger("civrealm_mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from civrealm_mcp.core import CivRealmGame, coerce_actor_id, jsonable  # noqa: E402

mcp = FastMCP("civrealm")

# All CivRealm/env work runs on this single thread (stable tornado IOLoop affinity, no
# already-running asyncio loop there).
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="civrealm-env")
_game = CivRealmGame()


async def _offload(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(fn, *args, **kwargs))


# --- status helpers -----------------------------------------------------------
def _players_summary() -> dict[str, Any]:
    out: dict[str, Any] = {}
    obs = _game.obs
    if isinstance(obs, dict) and isinstance(obs.get("player"), dict):
        for pid, p in obs["player"].items():
            if isinstance(p, dict):
                out[str(pid)] = {"name": jsonable(p.get("name")),
                                 "score": jsonable(p.get("score")),
                                 "is_alive": jsonable(p.get("is_alive"))}
    return out


def _status_dict() -> dict[str, Any]:
    status: dict[str, Any] = {
        "running": _game.running,
        "turn": jsonable(_game.turn),
        "step_count": _game.step_count,
        "last_reward": jsonable(_game.reward),
        "terminated": _game.terminated,
        "truncated": _game.truncated,
        "client_port": _game.client_port,
        "max_turns": _game.max_turns,
        "username": _game.username,
        "my_player_id": jsonable(_game.my_player_id()) if _game.env is not None else None,
        "players": _players_summary(),
    }
    if _game.env is not None and _game.done:
        results = _game.game_results()
        if results:
            status["game_results"] = jsonable(results)
            status["winner_player_ids"] = [str(pid) for pid, r in results.items()
                                           if isinstance(r, dict) and r.get("winner")]
    return status


# --- env operations (worker thread) -------------------------------------------
def _impl_start_game(client_port: int, max_turns: int, username: str) -> dict:
    obs, info = _game.start(client_port=client_port, max_turns=max_turns, username=username)
    status = _status_dict()
    avail = info.get("available_actions", {}) if isinstance(info, dict) else {}
    status["message"] = "game started"
    status["legal_action_actor_types"] = list(avail.keys())
    status["num_units"] = len(obs.get("unit", {})) if isinstance(obs, dict) else 0
    status["observation_keys"] = list(obs.keys()) if isinstance(obs, dict) else []
    return status


def _impl_take_action(ctrl_type: str, actor_id: int | str, action_name: str) -> dict:
    aid = coerce_actor_id(actor_id)
    if not _game.is_legal(ctrl_type, aid, action_name):
        return {
            "error": "illegal or unavailable action",
            "ctrl_type": ctrl_type, "actor_id": str(actor_id), "action_name": action_name,
            "valid_actions_for_actor": _game.legal_actions_for(ctrl_type, aid),
            "hint": "call get_legal_actions to see currently-valid actions",
        }
    _game.take_action(ctrl_type, aid, action_name)
    status = _status_dict()
    status["action_taken"] = [ctrl_type, jsonable(aid), action_name]
    return status


def _impl_end_turn() -> dict:
    _game.end_turn()
    status = _status_dict()
    status["action_taken"] = None
    status["message"] = "turn ended"
    return status


def _impl_close_game() -> dict:
    _game.close()
    return {"running": False, "message": "game closed"}


# --- MCP tools ----------------------------------------------------------------
@mcp.tool()
async def start_game(client_port: int = 6001, max_turns: int = 1000, username: str = "myagent") -> dict:
    """Start a new single-player CivRealm game vs the built-in AIs and return the initial status.

    Requires a running freeciv-web Docker server on http://localhost:8080 (see SETUP.md).

    Args:
        client_port: freeciv client port (default 6001; valid: 6001 or 6300-6331).
        max_turns: turn limit; truncated once turn > max_turns (default 1000).
        username: player name used to log in (no spaces/underscores).
    """
    return await _offload(_impl_start_game, client_port, max_turns, username)


@mcp.tool()
async def get_observation(keys: list[str] | None = None) -> dict:
    """Return the current observation as JSON (top-level keys: game, rules, map, player, city,
    tech, unit, options, dipl, gov, client; see SCHEMA.md). Pass `keys` to fetch only some
    sections (e.g. ["unit","player","city"]) and avoid the large 'map' arrays."""
    if _game.env is None or not _game.running:
        return {"error": "no game in progress; call start_game first"}
    obs = _game.obs
    if not isinstance(obs, dict):
        return {"observation": jsonable(obs), "note": "observation is empty (game over or defeated)"}
    if keys:
        selected = {k: obs[k] for k in keys if k in obs}
        result = {"observation": jsonable(selected)}
        missing = [k for k in keys if k not in obs]
        if missing:
            result["missing_keys"] = missing
        return result
    return {"observation": jsonable(obs), "observation_keys": list(obs.keys())}


@mcp.tool()
async def get_legal_actions(ctrl_type: str | None = None, actor_id: int | str | None = None) -> dict:
    """Return legal actions this step as {ctrl_type: {actor_id: {action_name: validity}}} (from
    info['available_actions']). Only actors that can currently act appear. An empty result means
    no actor can act (call end_turn)."""
    if _game.env is None or not _game.running:
        return {"error": "no game in progress; call start_game first"}
    avail = jsonable(_game.available_actions)
    if ctrl_type is not None:
        sub = avail.get(ctrl_type, {})
        if actor_id is not None:
            sub = sub.get(str(actor_id), {}) if isinstance(sub, dict) else {}
            return {"ctrl_type": ctrl_type, "actor_id": str(actor_id), "available_actions": sub}
        return {"ctrl_type": ctrl_type, "available_actions": sub}
    return {"available_actions": avail, "actor_types": list(avail.keys()),
            "note": "submit one via take_action(ctrl_type, actor_id, action_name); or end_turn()"}


@mcp.tool()
async def take_action(ctrl_type: str, actor_id: int | str, action_name: str) -> dict:
    """Perform ONE legal action for ONE actor: env.step((ctrl_type, actor_id, action_name)).
    Advances one step but does NOT end the turn — call end_turn() when done acting.

    Args:
        ctrl_type: one of unit, city, player, dipl, tech, gov.
        actor_id: the actor id (int for unit/city/player/gov/dipl; 'cur_player' for tech).
        action_name: the action key from get_legal_actions (e.g. 'build_city', 'goto_3').
    """
    if _game.env is None or not _game.running:
        return {"error": "no game in progress; call start_game first"}
    return await _offload(_impl_take_action, ctrl_type, actor_id, action_name)


@mcp.tool()
async def end_turn() -> dict:
    """End the current player's turn: env.step(None). Returns the updated status."""
    if _game.env is None or not _game.running:
        return {"error": "no game in progress; call start_game first"}
    return await _offload(_impl_end_turn)


@mcp.tool()
async def get_status() -> dict:
    """Return current game status: turn, per-player scores, terminated/truncated, my_player_id,
    and (once the game ends) the winner and final game results."""
    if _game.env is None:
        return {"running": False, "message": "no game in progress; call start_game first"}
    return _status_dict()


@mcp.tool()
async def close_game() -> dict:
    """Close the current game and disconnect the environment (env.close())."""
    if _game.env is None:
        return {"running": False, "message": "no game to close"}
    return await _offload(_impl_close_game)


# --- entry point --------------------------------------------------------------
def main() -> None:
    """Run the server over stdio, keeping JSON-RPC on the real stdout."""
    import anyio
    from mcp.server.stdio import stdio_server

    async def _run() -> None:
        sys.stdout = _REAL_STDOUT
        try:
            async with stdio_server() as (read_stream, write_stream):
                sys.stdout = sys.stderr
                await mcp._mcp_server.run(read_stream, write_stream,
                                          mcp._mcp_server.create_initialization_options())
        finally:
            sys.stdout = sys.stderr

    log.info("civrealm-mcp-server starting (stdio). Tools: start_game, get_observation, "
             "get_legal_actions, take_action, end_turn, get_status, close_game")
    anyio.run(_run)


if __name__ == "__main__":
    main()
