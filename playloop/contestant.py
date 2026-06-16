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
import os
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

CONSTRAINED_SYSTEM_PROMPT = """You are an expert player of Freeciv (a Civilization-style 4X \
strategy game), playing one turn at a time against built-in AI opponents.

You are shown your situation and, for each of your actors (units, cities, tech, government), a \
NUMBERED menu listing the ONLY actions available to that actor THIS turn. Option 0 is always \
"skip" (do nothing). Because you pick option numbers, you cannot make an illegal move.

Goal: build a thriving civilization — found cities with Settlers units, explore by moving units, \
improve terrain with Workers, research technology. Act with most units every turn; found cities \
when you can; don't leave units idle without reason.

Choose ONE option number for each actor you want to act (omit an actor, or choose 0, to skip it). \
Pick only numbers shown in that actor's menu.

Respond with ONLY a JSON object — no prose, no markdown, no code fences:
{"plan": "<one short sentence>", "choices": {"unit:104": 1, "city:110": 2, "tech:cur_player": 5}}
Keys are the actor ids exactly as shown (e.g. "unit:104"); values are the chosen option NUMBER \
from that actor's menu."""

DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
                    "Task", "NotebookEdit", "TodoWrite"]


@dataclass
class ChoiceResult:
    moves: list[dict] | None = None       # free-form mode: [{ctrl_type, actor_id, action}]
    choices: dict | None = None           # constrained mode: {actor_key: option_int}
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


def _norm_moves(parsed: dict) -> list:
    moves = parsed.get("moves", [])
    return moves if isinstance(moves, list) else []


def _norm_choices(parsed: dict) -> dict:
    raw = parsed.get("choices", {})
    norm: dict[str, object] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            norm[str(k)] = v
    elif isinstance(raw, list):  # tolerate [{"actor":..,"option":..}]
        for item in raw:
            if isinstance(item, dict):
                actor = item.get("actor") or item.get("actor_key")
                opt = item.get("option", item.get("choice", item.get("id")))
                if actor is not None:
                    norm[str(actor)] = opt
    return norm


class OllamaContestant:
    """Runs a small LOCAL model via Ollama — zero API usage, cross-platform (macOS/Linux/Windows;
    Ollama wraps llama.cpp, not MLX). Default: llama3.2:1b (Llama-3.2-1B-Instruct).

    Uses Ollama's chat API with format="json" (forces valid JSON output — important for a 1B model).
    Same interface as the other contestants: choose() / choose_constrained()."""

    def __init__(self, model: str = "llama3.2:1b", host: str | None = None,
                 max_tokens: int = 320, timeout: int = 180):
        self.model = model
        host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        if "://" not in host:
            host = "http://" + host
        self.host = host.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def name(self) -> str:
        return f"ollama:{self.model}"

    def _chat(self, system_prompt: str, user_prompt: str, fmt=None):
        import time

        import requests
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "stream": False,
            # fmt may be "json" (syntactic) or a full JSON Schema dict (structured-output /
            # grammar-constrained decoding — see representation.menu_to_schema).
            "format": fmt or "json",
            "options": {"temperature": 0, "num_predict": self.max_tokens},
        }
        t = time.time()
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        return content, int((time.time() - t) * 1000)

    def choose(self, state_text: str) -> ChoiceResult:
        try:
            out, ms = self._chat(SYSTEM_PROMPT, state_text)
        except Exception as e:
            return ChoiceResult(moves=[], error=f"ollama failed: {e!r}", cost_usd=0.0)
        parsed = _extract_json_object(out)
        if parsed is None:
            return ChoiceResult(moves=[], error="ollama: no JSON parsed", raw=out[:1000],
                                duration_ms=ms, cost_usd=0.0)
        return ChoiceResult(moves=_norm_moves(parsed), plan=str(parsed.get("plan", "")),
                            raw=out[:1000], duration_ms=ms, cost_usd=0.0)

    def choose_constrained(self, menu_text: str, schema=None) -> ChoiceResult:
        try:
            out, ms = self._chat(CONSTRAINED_SYSTEM_PROMPT, menu_text, fmt=schema)
        except Exception as e:
            return ChoiceResult(choices={}, error=f"ollama failed: {e!r}", cost_usd=0.0)
        parsed = _extract_json_object(out)
        if parsed is None:
            return ChoiceResult(choices={}, error="ollama: no JSON parsed", raw=out[:1000],
                                duration_ms=ms, cost_usd=0.0)
        return ChoiceResult(choices=_norm_choices(parsed), plan=str(parsed.get("plan", "")),
                            raw=out[:1000], duration_ms=ms, cost_usd=0.0)


class ClaudeSubagentContestant:
    """Spawns `claude -p` per decision; returns the parsed moves."""

    def __init__(self, model: str = "claude-opus-4-8", timeout: int = 300):
        self.model = model
        self.timeout = timeout

    def name(self) -> str:
        return f"claude-subagent:{self.model}"

    def _invoke(self, prompt: str, system_prompt: str):
        """Run one `claude -p` subagent. Returns (parsed_json|None, error|None, raw, ms, cost)."""
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", self.model,
               "--system-prompt", system_prompt, "--disallowed-tools", *DISALLOWED_TOOLS]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return None, f"subagent timeout after {self.timeout}s", "", None, None
        except FileNotFoundError:
            return None, "claude CLI not found on PATH", "", None, None
        if proc.returncode != 0:
            return None, f"claude rc={proc.returncode}: {proc.stderr[-300:]}", proc.stdout[-500:], None, None
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None, "could not parse claude envelope", proc.stdout[-500:], None, None
        result_text = env.get("result", "")
        duration, cost = env.get("duration_ms"), env.get("total_cost_usd")
        if env.get("is_error"):
            return None, f"claude is_error: {result_text[:300]}", result_text, duration, cost
        parsed = _extract_json_object(result_text)
        if parsed is None:
            return None, "could not parse JSON from result", result_text[:1000], duration, cost
        return parsed, None, result_text[:1000], duration, cost

    def choose(self, state_text: str) -> ChoiceResult:
        """Free-form mode: returns moves [{ctrl_type, actor_id, action}]."""
        parsed, error, raw, ms, cost = self._invoke(state_text, SYSTEM_PROMPT)
        if parsed is None:
            return ChoiceResult(moves=[], error=error, raw=raw, duration_ms=ms, cost_usd=cost)
        moves = parsed.get("moves", [])
        if not isinstance(moves, list):
            moves = []
        return ChoiceResult(moves=moves, plan=str(parsed.get("plan", "")), raw=raw,
                            duration_ms=ms, cost_usd=cost)

    def choose_constrained(self, menu_text: str, schema=None) -> ChoiceResult:
        """Constrained mode: returns choices {actor_key: option_int} over a per-actor menu.
        `schema` is accepted for interface parity but not enforced here (the `claude` CLI path
        doesn't pass output_config; the menu text already enumerates the valid options)."""
        parsed, error, raw, ms, cost = self._invoke(menu_text, CONSTRAINED_SYSTEM_PROMPT)
        if parsed is None:
            return ChoiceResult(choices={}, error=error, raw=raw, duration_ms=ms, cost_usd=cost)
        raw_choices = parsed.get("choices", {})
        norm: dict[str, object] = {}
        if isinstance(raw_choices, dict):
            for k, v in raw_choices.items():
                norm[str(k)] = v
        elif isinstance(raw_choices, list):  # tolerate [{"actor":..,"option":..}]
            for item in raw_choices:
                if isinstance(item, dict):
                    actor = item.get("actor") or item.get("actor_key")
                    opt = item.get("option", item.get("choice", item.get("id")))
                    if actor is not None:
                        norm[str(actor)] = opt
        return ChoiceResult(choices=norm, plan=str(parsed.get("plan", "")), raw=raw,
                            duration_ms=ms, cost_usd=cost)
