#!/usr/bin/env python
"""
Acceptance CHECK: drive ONE CivRealm game PURELY through the MCP tools.

Not a unit test and deliberately not named `test_*`: it needs a live freeciv-web Docker
server, it takes real game turns, and it asserts nothing — it PRINTS the state at each step
for a human to read. `pytest` must not try to collect it, which is why it lives in scripts/
rather than under a tests/ directory. The offline suites pytest does collect are
playloop/test_*.py.

This is an MCP *client*. It spawns the civrealm-mcp-server as a stdio subprocess,
then exercises the required flow with no GUI and no direct civrealm calls:

    list tools -> start_game -> get_observation -> get_legal_actions
               -> take_action (a legal one) -> end_turn -> get_status -> close_game

It prints before/after state at each step.

Prereqs: the freeciv-web Docker server must be running on http://localhost:8080
(see SETUP.md), and this must run inside the project venv.

Run:  .venv/bin/python scripts/acceptance_check.py
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PYTHON = sys.executable  # the venv python running this script


def payload(result) -> dict:
    """Extract the tool's JSON return from a CallToolResult."""
    if getattr(result, "isError", False):
        texts = [getattr(c, "text", "") for c in result.content]
        return {"_isError": True, "content": texts}
    # FastMCP serializes the dict return into a TextContent JSON string.
    for c in result.content:
        text = getattr(c, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    return {"_unparsed": True}


def show(title: str, data) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
    print(json.dumps(data, indent=2, default=str)[:4000])


def pick_legal_action(available: dict):
    """Choose a legal (ctrl_type, actor_id, action_name); prefer a unit doing something useful."""
    order = ["unit", "city", "tech", "gov", "player", "dipl"]
    for ctrl_type in order + [k for k in available if k not in order]:
        actors = available.get(ctrl_type) or {}
        for actor_id, action_map in actors.items():
            valid = [name for name, ok in action_map.items() if ok]
            if not valid:
                continue
            # Prefer a meaningful action when present.
            for preferred in ("build_city",):
                if preferred in valid:
                    return ctrl_type, actor_id, preferred
            goto = [v for v in valid if v.startswith("goto_")]
            return ctrl_type, actor_id, (goto[0] if goto else valid[0])
    return None


async def main(client_port: int = 6001, max_turns: int = 4) -> int:
    server_params = StdioServerParameters(
        command=PYTHON,
        args=["-m", "civrealm_mcp.server"],
        env=os.environ.copy(),
    )

    print("Spawning civrealm-mcp-server over stdio and driving a game via MCP tools only...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            show("MCP TOOLS EXPOSED BY THE SERVER", [t.name for t in tools.tools])

            # 1) start_game
            res = await session.call_tool("start_game", {"client_port": client_port, "max_turns": max_turns})
            started = payload(res)
            show("start_game -> initial status (BEFORE any action)", started)
            if started.get("_isError"):
                print("\nstart_game failed; is the freeciv-web Docker server running on :8080?")
                return 1

            # 2) get_observation (skip the huge 'map' arrays; show player/unit/city)
            res = await session.call_tool("get_observation", {"keys": ["player", "unit", "city"]})
            obs = payload(res)
            show("get_observation(keys=['player','unit','city'])", obs)

            # 3) get_legal_actions
            res = await session.call_tool("get_legal_actions", {})
            legal = payload(res)
            show("get_legal_actions -> full legal-action mask", legal)

            available = legal.get("available_actions", {})
            choice = pick_legal_action(available)
            if not choice:
                print("\nNo legal actor actions this step; ending turn instead.")
                res = await session.call_tool("end_turn", {})
                show("end_turn (no actions available)", payload(res))
                res = await session.call_tool("get_status", {})
                show("get_status (final)", payload(res))
                await session.call_tool("close_game", {})
                return 0

            ctrl_type, actor_id, action_name = choice
            res = await session.call_tool("get_status", {})
            before = payload(res)
            show("get_status BEFORE take_action", before)

            # 4) take_action (a legal one)
            print(f"\n>>> taking legal action: ({ctrl_type}, {actor_id}, {action_name})")
            res = await session.call_tool(
                "take_action",
                {"ctrl_type": ctrl_type, "actor_id": str(actor_id), "action_name": action_name},
            )
            after_action = payload(res)
            show("take_action -> status AFTER action", after_action)

            # 5) end_turn
            res = await session.call_tool("end_turn", {})
            after_endturn = payload(res)
            show("end_turn -> status AFTER ending turn", after_endturn)

            # 6) get_status
            res = await session.call_tool("get_status", {})
            show("get_status -> FINAL status", payload(res))

            # cleanup
            res = await session.call_tool("close_game", {})
            show("close_game", payload(res))

            # tiny before/after summary
            print(f"\n{'=' * 78}\nBEFORE/AFTER SUMMARY\n{'-' * 78}")
            print(f"  turn:  start={started.get('turn')}  "
                  f"after_action={after_action.get('turn')}  after_end_turn={after_endturn.get('turn')}")
            print(f"  step_count: start={started.get('step_count')}  "
                  f"after_action={after_action.get('step_count')}  after_end_turn={after_endturn.get('step_count')}")
            print(f"  action taken: {after_action.get('action_taken')}")
            print("\nACCEPTANCE TEST COMPLETE — all steps driven purely through MCP tools.")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Drive a CivRealm game purely through MCP tools")
    ap.add_argument("--port", type=int, default=6001, help="freeciv client_port (default 6001)")
    ap.add_argument("--max-turns", type=int, default=4)
    a = ap.parse_args()
    raise SystemExit(anyio.run(main, a.port, a.max_turns))
