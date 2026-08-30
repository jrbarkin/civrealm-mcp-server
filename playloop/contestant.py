"""
The contestant behind one interface: a shaped prompt -> a chosen set of moves.

Default implementation spawns an isolated `claude` CLI subagent per turn. This uses the
environment's existing Claude auth (no ANTHROPIC_API_KEY needed) and gives each decision a
fresh context with the file, shell and web tools denied (`DISALLOWED_TOOLS` — a denylist, not a
bare model). Swap in any object exposing `.choose_constrained(menu_text, schema=None)` (constrained
mode, the default) and/or `.choose(state_text)` (free-form mode), each returning a `ChoiceResult`.

For THIS viability probe the default model is Claude Opus 4.8 (`playloop.loop`'s default) so
that if it flails, the bug is our representation/loop, not the model.

Three contestants, and they do NOT enforce the per-turn schema the same way — see each class:

  OllamaContestant        schema -> the wire `format` field -> llama.cpp grammar-constrained
                          decoding. An out-of-enum option cannot be SAMPLED.
  AnthropicSDKContestant  schema -> `output_config.format` on the Messages API, which compiles
                          the schema server-side. Same guarantee; needs an API key.
  ClaudeSubagentContestant  schema -> the `claude` CLI's `--json-schema`, which is
                          validate-and-RETRY at the harness layer, not constrained decoding:
                          the model can emit an out-of-enum value, the CLI rejects it and asks
                          again. Strictly weaker than the two above. Measured, not assumed.
"""

from __future__ import annotations

import json
import os
import random
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

Choose ONE option number for EVERY actor listed — use 0 to skip one. Do not leave an actor out: \
the reply must name every actor shown. Pick only numbers shown in that actor's menu.

Respond with ONLY a JSON object — no prose, no markdown, no code fences:
{"plan": "<one short sentence>", "choices": {"unit:104": 1, "city:110": 2, "tech:cur_player": 5}}
Keys are the actor ids exactly as shown (e.g. "unit:104") and must cover ALL of them; values are \
the chosen option NUMBER from that actor's menu (0 = skip)."""

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
    """Pull a JSON **object** out of a model reply. Returns None for anything else.

    The isinstance guards are not decoration: `json.loads` is happy to return a list, string or
    number for a well-formed reply like `[{"unit:101": 1}]`, and every caller immediately does
    `parsed.get(...)`. Without the guard that is an AttributeError that kills the run, instead of
    a "no JSON parsed" that the retry loop can recover from."""
    text = (text or "").strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return parsed if isinstance(parsed, dict) else None
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
                    parsed = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
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
    Same interface as the other contestants: choose() / choose_constrained().

    `think=False` is REQUIRED for the 4B gameplay models (qwen3:4b, gemma4:e4b). They are
    "thinking" models that, left on, spend the entire num_predict budget on the hidden
    `message.thinking` channel and return an EMPTY `message.content` (done_reason=length) — i.e.
    no answer at all. Disabling thinking makes them emit the JSON answer directly (and far
    faster: qwen3:4b 24s -> 2.5s). It is a harmless no-op on non-thinking models (llama3.2:1b)."""

    def __init__(self, model: str = "llama3.2:1b", host: str | None = None,
                 max_tokens: int = 320, timeout: int = 180, think: bool = False):
        self.model = model
        host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        if "://" not in host:
            host = "http://" + host
        self.host = host.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.think = think

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
            # Disable the reasoning channel so the model spends tokens on the answer, not on
            # hidden thinking (the 4B models return empty content otherwise — see class docstring).
            "think": self.think,
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
    """Spawns `claude -p` per decision; returns the parsed moves.

    Schema handling (constrained mode): the per-turn schema is passed to the CLI's
    `--json-schema`, which makes the CLI expose a `StructuredOutput` tool, VALIDATE the model's
    call against the schema, and feed any validation error back for another attempt. That is
    enforcement by the harness, NOT grammar-constrained decoding — verified directly: prompted
    to pick an out-of-enum option the model emitted `{"unit:101": 99}`, the CLI answered
    "Output does not match required schema: /choices/unit:101: must be equal to one of the
    allowed values", and the turn ended with `structured_output: null`. So on this backend an
    illegal option is *rejected*, not *unrepresentable*.

    It still earns its place. Measured over one real committed menu (8 actors, 3 runs each,
    claude-haiku-4-5): without the schema 2 of 3 replies silently OMITTED an actor; with it,
    3 of 3 returned all 8 actors, none out of enum — the schema's `required` is what closes
    that gap. Cost: roughly 2x wall-clock per turn."""

    def __init__(self, model: str = "claude-opus-4-8", timeout: int = 300):
        self.model = model
        self.timeout = timeout

    def name(self) -> str:
        return f"claude-subagent:{self.model}"

    def _invoke(self, prompt: str, system_prompt: str, schema=None):
        """Run one `claude -p` subagent. Returns (parsed_json|None, error|None, raw, ms, cost)."""
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", self.model,
               "--system-prompt", system_prompt, "--disallowed-tools", *DISALLOWED_TOOLS]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
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
        if schema is not None:
            # With --json-schema the CLI returns the object it already validated against the
            # schema. `null` means the model never produced a conforming one (it gave up after
            # the CLI's own retries) — surface that as an error so loop.resolve_choices retries
            # with the valid options enumerated, rather than silently acting on nothing.
            structured = env.get("structured_output")
            if isinstance(structured, dict):
                return structured, None, result_text[:1000], duration, cost
            return (None, "claude: no schema-conforming output (structured_output null)",
                    result_text[:1000], duration, cost)
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
        `schema` is forwarded to the CLI's `--json-schema` (validated + retried by the CLI —
        see the class docstring for what that does and does not guarantee)."""
        parsed, error, raw, ms, cost = self._invoke(menu_text, CONSTRAINED_SYSTEM_PROMPT,
                                                    schema=schema)
        if parsed is None:
            return ChoiceResult(choices={}, error=error, raw=raw, duration_ms=ms, cost_usd=cost)
        return ChoiceResult(choices=_norm_choices(parsed), plan=str(parsed.get("plan", "")),
                            raw=raw, duration_ms=ms, cost_usd=cost)


class AnthropicSDKContestant:
    """Same decision interface, but over the Anthropic Messages API instead of the `claude` CLI,
    so the per-turn schema reaches `output_config.format` — the API compiles the schema and
    constrains generation to it, the same guarantee `OllamaContestant` gets from llama.cpp and
    the one the CLI path does NOT get (see ClaudeSubagentContestant).

    Verified against the API, but NOT yet in a full game: single turns only, no committed run
    directory. What was checked (anthropic 0.109.1, claude-haiku-4-5, a real committed 8-actor
    menu from `runs/constrained-smoke2` and the schema `menu_to_schema` builds from it):
      - the real per-turn schema is accepted; 8/8 actors returned, every value in its enum;
      - schema-vs-prompt conflict, 3/3 runs: with every actor's enum narrowed to `[1]` and the
        prompt ordering "answer 7 for every single actor", the reply was `1` for all 8 actors
        every time. The instruction could not be obeyed because the value could not be emitted.
    `--backend claude` stays the DEFAULT anyway: it needs no API key, and it is the backend the
    committed runs were produced on. Use `--backend api` when the decoder-level guarantee is
    the point.

    Cost is NOT reported (ChoiceResult.cost_usd stays None, so a run's `total_cost_usd` reads 0
    on this backend); token usage is recorded in the transcript's `raw` field instead, because
    turning tokens into dollars would put a number in the output that no committed artifact
    backs."""

    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 2048,
                 timeout: int = 300):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._client = None

    def name(self) -> str:
        return f"anthropic-api:{self.model}"

    def _get_client(self):
        if self._client is None:
            import anthropic  # imported lazily: the other two backends don't need the SDK
            self._client = anthropic.Anthropic(timeout=self.timeout)
        return self._client

    @staticmethod
    def _api_schema(schema: dict) -> dict:
        """Anthropic's structured outputs want every object closed and every property `required`.
        `menu_to_schema` already sets `additionalProperties: false` on both levels and requires
        every actor inside `choices`; the only gap is the top-level optional `plan`. Close it
        HERE, not in `menu_to_schema`, so the schema the measured Ollama and CLI runs were given
        stays byte-identical — this backend adapts to its API rather than changing the artifact."""
        return {**schema, "required": list(schema.get("properties") or {})}

    def _message(self, system_prompt: str, user_prompt: str, schema=None):
        """One Messages API call. Returns (parsed|None, error|None, raw, ms)."""
        import time
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema",
                                                  "schema": self._api_schema(schema)}}
        t = time.time()
        try:
            resp = self._get_client().messages.create(**kwargs)
        except Exception as e:
            return None, f"anthropic api failed: {e!r}", "", int((time.time() - t) * 1000)
        ms = int((time.time() - t) * 1000)
        usage = getattr(resp, "usage", None)
        raw = (f"[usage in={getattr(usage, 'input_tokens', None)} "
               f"out={getattr(usage, 'output_tokens', None)} stop={resp.stop_reason}] ")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        raw += text[:1000]
        if resp.stop_reason == "refusal":
            return None, "anthropic api: refusal (schema not guaranteed)", raw, ms
        parsed = _extract_json_object(text)
        if parsed is None:
            return None, "anthropic api: no JSON parsed", raw, ms
        return parsed, None, raw, ms

    def choose(self, state_text: str) -> ChoiceResult:
        parsed, error, raw, ms = self._message(SYSTEM_PROMPT, state_text)
        if parsed is None:
            return ChoiceResult(moves=[], error=error, raw=raw, duration_ms=ms)
        return ChoiceResult(moves=_norm_moves(parsed), plan=str(parsed.get("plan", "")),
                            raw=raw, duration_ms=ms)

    def choose_constrained(self, menu_text: str, schema=None) -> ChoiceResult:
        parsed, error, raw, ms = self._message(CONSTRAINED_SYSTEM_PROMPT, menu_text, schema=schema)
        if parsed is None:
            return ChoiceResult(choices={}, error=error, raw=raw, duration_ms=ms)
        return ChoiceResult(choices=_norm_choices(parsed), plan=str(parsed.get("plan", "")),
                            raw=raw, duration_ms=ms)


class RandomContestant:
    """Deterministic, model-free test fixture for orchestration testing.

    Picks a uniformly-random valid option number for each actor, read from the per-turn JSON
    Schema's enums (representation.menu_to_schema). No env access, no LLM — so any wrong
    orchestration result is unambiguously the orchestrator's fault, not a model's. Seeded for
    reproducibility (same seed -> same choices given the same menus).

    Constrained mode only: the orchestrator never calls the free-form path, so there is no
    `choose()` here."""

    def __init__(self, seed: int = 0):
        self.seed = seed
        self.rng = random.Random(seed)

    def name(self) -> str:
        return f"random:{self.seed}"

    def choose_constrained(self, menu_text: str, schema=None) -> ChoiceResult:
        choices: dict[str, int] = {}
        props = ((schema or {}).get("properties", {}).get("choices", {}).get("properties", {}))
        for actor_key, spec in props.items():
            enum = spec.get("enum") or [0]
            choices[actor_key] = self.rng.choice(enum)
        return ChoiceResult(choices=choices, plan="random", cost_usd=0.0, duration_ms=0)
