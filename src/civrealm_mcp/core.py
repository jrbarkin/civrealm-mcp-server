"""
Shared CivRealm env-access core.

Both the MCP server (src/civrealm_mcp/server.py) and the headless single-agent play loop
(playloop/) drive the environment through this one layer, so behaviour can't drift between
the debug surface (MCP) and the experiment (play loop).

This module is synchronous. The MCP server runs it on a dedicated worker thread (CivRealm
drives a tornado IOLoop inside reset()/step(), which collides with FastMCP's asyncio loop);
the headless play loop has no running event loop and calls it directly.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("civrealm_mcp.core")

# goto_<dir> action keys -> readable compass directions (from civrealm
# configs/llm_wrapper_settings.yaml, the authoritative mapping).
GOTO_TO_COMPASS = {
    "goto_0": "move_NorthWest", "goto_1": "move_North", "goto_2": "move_NorthEast",
    "goto_3": "move_West", "goto_4": "move_East",
    "goto_5": "move_SouthWest", "goto_6": "move_South", "goto_7": "move_SouthEast",
}
COMPASS_TO_GOTO = {v: k for k, v in GOTO_TO_COMPASS.items()}


def jsonable(x: Any) -> Any:
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
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (set, frozenset)):
        return [jsonable(v) for v in sorted(x, key=str)]
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def coerce_actor_id(actor_id: int | str) -> int | str:
    """unit/city/player/gov/dipl ids are ints; tech uses the string 'cur_player'."""
    if isinstance(actor_id, int):
        return actor_id
    s = str(actor_id)
    if s.lstrip("-").isdigit():
        return int(s)
    return s


class CivRealmGame:
    """Owns exactly one FreecivBaseEnv and exposes thin start/observe/act/end-turn helpers."""

    def __init__(self) -> None:
        self.env: Any = None
        self.obs: Any = None
        self.info: Any = None
        self.reward: Any = None
        self.terminated: bool | None = None
        self.truncated: bool | None = None
        self.step_count: int = 0
        self.client_port: int | None = None
        self.max_turns: int | None = None
        self.username: str | None = None
        self.running: bool = False

    # --- lifecycle ---------------------------------------------------------
    def start(self, client_port: int = 6001, max_turns: int = 1000,
              username: str = "myagent", begin_turn_timeout: int = 120,
              minp: int = 1, seed: int | None = None):
        """Create/reset a game and return (obs, info).

        minp=1 -> single-player vs built-in AIs (starts immediately). minp>=2 -> wait for that many
        human players on the same client_port (two-agent orchestration). seed (if given) fixes the
        map/game/agent seeds for reproducible games. The per-client identity is the constructor
        `username` (login); fc_args is set for shared game settings (idempotent across clients)."""
        from civrealm.configs import fc_args
        from civrealm.envs.freeciv_base_env import FreecivBaseEnv

        if self.env is not None:
            self.close()

        fc_args["username"] = username
        fc_args["max_turns"] = max_turns
        fc_args["minp"] = minp
        fc_args["begin_turn_timeout"] = max(fc_args.get("begin_turn_timeout", 30), begin_turn_timeout)
        if seed is not None:
            fc_args["debug.randomly_generate_seeds"] = False
            fc_args["debug.mapseed"] = seed
            fc_args["debug.gameseed"] = seed
            fc_args["debug.agentseed"] = seed

        log.info("start: FreecivBaseEnv(username=%s) port=%s max_turns=%s minp=%s seed=%s",
                 username, client_port, max_turns, minp, seed)
        self.env = FreecivBaseEnv(username=username)
        obs, info = self.env.reset(client_port=client_port)
        self.obs, self.info = obs, info
        self.reward, self.terminated, self.truncated, self.step_count = 0, False, False, 0
        self.client_port, self.max_turns, self.username, self.running = client_port, max_turns, username, True
        return obs, info

    def close(self) -> None:
        if self.env is not None:
            try:
                self.env.close()
            except Exception as e:  # pragma: no cover
                log.warning("close error: %r", e)
        self.env = None
        self.running = False

    # --- state -------------------------------------------------------------
    @property
    def turn(self) -> int | None:
        return self.info.get("turn") if isinstance(self.info, dict) else None

    @property
    def available_actions(self) -> dict:
        return self.info.get("available_actions", {}) if isinstance(self.info, dict) else {}

    def legal_actions_for(self, ctrl_type: str, actor_id: int | str) -> list[str]:
        aid = coerce_actor_id(actor_id)
        sub = self.available_actions.get(ctrl_type, {})
        actor = sub.get(aid, {}) if isinstance(sub, dict) else {}
        return [k for k, v in actor.items() if v]

    def is_legal(self, ctrl_type: str, actor_id: int | str, action_name: str) -> bool:
        return action_name in self.legal_actions_for(ctrl_type, actor_id)

    # --- mutation ----------------------------------------------------------
    def take_action(self, ctrl_type: str, actor_id: int | str, action_name: str):
        aid = coerce_actor_id(actor_id)
        result = self.env.step((ctrl_type, aid, action_name))
        self._update(*result)
        return result

    def end_turn(self):
        result = self.env.step(None)
        self._update(*result)
        return result

    def _update(self, obs, reward, terminated, truncated, info) -> None:
        self.obs, self.reward, self.info = obs, reward, info
        self.terminated, self.truncated = bool(terminated), bool(truncated)
        self.step_count += 1

    @property
    def done(self) -> bool:
        return bool(self.terminated or self.truncated)

    # --- convenience -------------------------------------------------------
    def my_player_id(self):
        try:
            return self.env.get_playerid()
        except Exception:
            return None

    def game_results(self) -> dict:
        try:
            return self.env.get_game_results() or {}
        except Exception:
            return {}
