# civrealm-mcp-server

A **minimal [Model Context Protocol](https://modelcontextprotocol.io) server** that exposes a
**single [CivRealm](https://github.com/bigai-ai/civrealm) (Freeciv) Gymnasium environment** as
tools, so an external MCP agent can drive one game **by hand** over stdio — no GUI, and **no LLM
inside the server** (the MCP client is the decision-maker).

This is an exploratory first pass toward LLM-vs-LLM Freeciv. Scope here is intentionally tiny:
**one env, exposed via MCP, drivable by hand.** No tournament/rating/two-agent orchestration.

## What it exposes

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

The CivRealm observation/action/lifecycle contract is documented in **[SCHEMA.md](SCHEMA.md)**.

## Quick start

Full, reproduced steps (with the macOS/Apple-Silicon fixes) are in **[SETUP.md](SETUP.md)**. In short:

```bash
# 1. freeciv-web Docker server (publish only 8080 — avoids the macOS AirPlay :7000 conflict)
docker run -d --name freeciv-web --rm -p 8080:80 freeciv/freeciv-web:latest
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/        # -> 200

# 2. Python 3.11 env with civrealm + this server (see SETUP.md for setuptools<81 fix)
uv venv --python 3.11 .venv
uv pip install --python .venv -e /path/to/civrealm 'setuptools<81'
uv pip install --python .venv --no-deps -e .

# 3. drive a game purely through MCP tools
.venv/bin/python tests/acceptance_test.py
```

Wire it into an MCP client (e.g. Claude Desktop):

```json
{ "mcpServers": { "civrealm": {
    "command": "/Users/jbarkin/Repos/civrealm-mcp-server/.venv/bin/civrealm-mcp-server" } } }
```

## How it works (and how it differs from vanilla CivRealm)

CivRealm out of the box is a Python Gymnasium library — you drive it by writing Python
(`gymnasium.make`, `env.reset/step`) and, in the baseline, by baking an LLM into that process.
This server wraps that **same single env** behind MCP/JSON-RPC so **any** MCP client can play by
calling tools, with the model living **outside** the server. It reimplements no game logic.

Two implementation details that matter (both discovered by running it):
- **stdio hygiene:** CivRealm `print()`s to stdout; stdio MCP uses stdout for JSON-RPC. The
  server routes all non-protocol output to stderr and hands only the real stdout to the transport.
- **event loop:** CivRealm drives a tornado `IOLoop` inside `reset()/step()`; FastMCP runs tools
  in the asyncio loop. The server offloads every env call to one dedicated worker thread.

## Project layout

```
src/civrealm_mcp/server.py   # the MCP server (7 tools)
tests/acceptance_test.py     # Phase-3 acceptance test: drives a game purely via MCP tools
SCHEMA.md                    # observation / action / lifecycle schema (primary deliverable)
SETUP.md                     # exact setup steps that worked, incl. fixes
STATUS.md                    # what works, what doesn't, recommended next steps
```

## Status & scope

See **[STATUS.md](STATUS.md)**. Verified end-to-end on macOS arm64: server starts, a game runs,
and a game is driven start→observe→legal-actions→take-action→end-turn→status purely through MCP
tools. Out of scope (per the task): tournament/bracket, Elo, two-agent orchestration, or any
LLM in the loop.
