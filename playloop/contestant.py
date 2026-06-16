"""
The contestant behind one interface: a shaped prompt -> a chosen set of moves.

Default implementation spawns an isolated `claude` CLI subagent per turn. This uses the
environment's existing Claude auth (no ANTHROPIC_API_KEY needed) and gives each decision a
fresh, tool-less context. Swap in any object exposing `.choose(state_text) -> ChoiceResult`.

For THIS viability probe we run a strong model (Claude Opus 4.8) so that if it flails, the
bug is our representation/loop, not the model.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

SYSTEM_PROMPT = """You are an expert player of Freeciv (a Civilization-style 4X strategy game), \
playing one turn at a time against built-in AI opponents.

Your goal: build a thriving civilization — found cities with Settlers units (the `build_city` \
action), explore the map by moving units, improve terrain with Workers, research technology, \
and grow your score. Early game priorities: found your first city quickly with a Settlers unit, \
move other units to explore, and set research.

Each turn you receive your current state and, for each of your actors (units, cities, tech, \
government), the EXACT list of legal action keys you may choose from THIS turn. Rules:
- Choose at most one action per actor. You may skip an actor by omitting it.
- Use the action's EXACT key as shown — the token BEFORE any [bracket]. For movement, return \
e.g. "goto_1", NOT "move_North".
- Only choose keys that appear in that actor's list. Acting is better than idling: move or use \
most units every turn, and found cities when you can.
- To leave a unit where it is, use the HOLD action that appears in THAT unit's list — usually \
"keep_activity" (annotated [hold: ...]). Do NOT use "fortify" or "sentry" unless that exact key \
appears in that unit's list; many units cannot fortify on a given turn.

Respond with ONLY a JSON object — no prose, no markdown, no code fences:
{"plan": "<one short sentence on your intent this turn>", "moves": [{"ctrl_type": "unit", \
"actor_id": "101", "action": "build_city"}]}
"moves" may be empty only if genuinely nothing is worth doing."""

DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
                    "Task", "NotebookEdit", "TodoWrite"]


@dataclass
class ChoiceResult:
    moves: list[dict]
    plan: str = ""
    raw: str = ""
    error: str | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None


def _extract_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start == -1:
        return None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class ClaudeSubagentContestant:
    """Spawns `claude -p` per decision; returns the parsed moves."""

    def __init__(self, model: str = "claude-opus-4-8", timeout: int = 300):
        self.model = model
        self.timeout = timeout

    def name(self) -> str:
        return f"claude-subagent:{self.model}"

    def choose(self, state_text: str) -> ChoiceResult:
        cmd = [
            "claude", "-p", state_text,
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", SYSTEM_PROMPT,
            "--disallowed-tools", *DISALLOWED_TOOLS,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return ChoiceResult(moves=[], error=f"subagent timeout after {self.timeout}s")
        except FileNotFoundError:
            return ChoiceResult(moves=[], error="claude CLI not found on PATH")

        if proc.returncode != 0:
            return ChoiceResult(moves=[], error=f"claude rc={proc.returncode}: {proc.stderr[-300:]}",
                                raw=proc.stdout[-500:])

        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ChoiceResult(moves=[], error="could not parse claude envelope", raw=proc.stdout[-500:])

        result_text = env.get("result", "")
        duration = env.get("duration_ms")
        cost = env.get("total_cost_usd")
        if env.get("is_error"):
            return ChoiceResult(moves=[], error=f"claude is_error: {result_text[:300]}",
                                raw=result_text, duration_ms=duration, cost_usd=cost)

        parsed = _extract_json_object(result_text)
        if parsed is None:
            return ChoiceResult(moves=[], error="could not parse JSON moves from result",
                                raw=result_text[:1000], duration_ms=duration, cost_usd=cost)

        moves = parsed.get("moves", [])
        if not isinstance(moves, list):
            moves = []
        return ChoiceResult(moves=moves, plan=str(parsed.get("plan", "")),
                            raw=result_text[:1000], duration_ms=duration, cost_usd=cost)
