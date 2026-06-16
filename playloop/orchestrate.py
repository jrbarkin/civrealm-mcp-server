"""
Two-agent orchestration: two contestants, ONE shared CivRealm game, a decided winner.

Reuses the proven two-player mechanism (minp=2, shared client_port, two client connections), but
in a single process. The hard part is the turn handoff: env calls (reset/step/end_turn) BLOCK
(they drive a tornado IOLoop), and in a 2-player game they block on each other —
`gameB.reset()` doesn't return until A ends its first phase, and `A.end_turn()` doesn't return
until B finishes its phase. So a single shared worker thread would deadlock.

Design:
- **One thread per side.** Each side's game owns its own thread, so both `reset()` calls can block
  concurrently until the game starts, and one side's blocking `end_turn` doesn't stall the other.
- **Serialized handoff is enforced by freeciv's sequential phases**: side B's thread stays blocked
  inside `start()`/`end_turn()` until it is B's phase, so only the "active" side is ever between its
  observation and its `end_turn`. A `query_lock` additionally guarantees only ONE contestant is
  queried at a time (the explicit "only one brain at a time" requirement).
- **Anti-hang:** `begin_turn_timeout` makes a stuck `end_turn` return (truncated) instead of
  hanging forever; the orchestrator also `join`s each thread with a timeout and reports a hang.
- **Determinism:** the game is seeded (map/game/agent) and each RandomContestant is seeded, so
  same seeds → identical game → identical winner. Phases are strictly sequential, so thread timing
  doesn't change the outcome.

Winner: `decide_winner` over both players' final scores (read at the cap), mapped back to the side.

Usage:
    .venv/bin/python -m playloop.orchestrate --cap 6 --seed 42 --side-a random:1 --side-b random:2
    # side specs: random:<seed> | local:<ollama-model> | claude:<model>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time

from civrealm_mcp.core import CivRealmGame
from playloop.contestant import (ClaudeSubagentContestant, OllamaContestant, RandomContestant)
from playloop.loop import apply_constrained, resolve_choices
from playloop.representation import build_choice_menu, menu_to_schema, render_menu
from playloop.winrule import decide_winner, get_final_result

_log_lock = threading.Lock()
_log_lines: list[str] = []


def _log(msg: str) -> None:
    with _log_lock:
        _log_lines.append(msg)
    print(msg, file=sys.stderr, flush=True)


def winner_label_for(verdict: dict, side_a_spec: str, side_b_spec: str) -> str:
    """PURE. Map decide_winner's 'A'/'B'/'tie' back to the correct side's spec label."""
    return {"A": side_a_spec, "B": side_b_spec,
            "tie": "tie (tiebreak exhausted)"}[verdict["winner"]]


def make_contestant(spec: str):
    """spec: 'random:<seed>' | 'local:<ollama-model>' | 'claude:<model>'."""
    backend, _, rest = spec.partition(":")
    if backend == "random":
        return RandomContestant(seed=int(rest) if rest else 0)
    if backend == "local":
        return OllamaContestant(model=rest or "llama3.2:1b")
    if backend == "claude":
        return ClaudeSubagentContestant(model=rest or "claude-opus-4-8")
    raise ValueError(f"unknown side spec: {spec!r}")


def _run_side(side: str, game: CivRealmGame, contestant, username: str, client_port: int,
              max_turns: int, seed: int, query_lock: threading.Lock,
              results: dict, errors: dict) -> None:
    try:
        game.start(client_port=client_port, max_turns=max_turns, username=username,
                   minp=2, seed=seed, begin_turn_timeout=120)
        pid = game.my_player_id()
        _log(f"[{side}] joined as player_id={pid} (username={username}) at turn {game.turn}")
        turns_acted = 0
        while not game.done:
            turn = game.turn
            if turn is None or turn > max_turns:
                break
            menu = build_choice_menu(game)
            schema = menu_to_schema(menu)
            with query_lock:  # only one contestant queried at a time
                choices, _meta = resolve_choices(contestant, menu, render_menu(menu), schema=schema)
            ap = apply_constrained(game, menu, choices)
            turns_acted += 1
            _log(f"[{side}] player{pid} TURN {turn}: applied={ap['applied']} illegal={ap['illegal']} "
                 f"stale={ap.get('stale', 0)} families={dict(ap['families'])}")
            if not game.done:
                game.end_turn()  # blocks until this side's next phase (other side acts meanwhile)
        results[side] = {
            "player_id": pid, "contestant": contestant.name(),
            "turns_acted": turns_acted, "final_turn": game.turn,
            "final_result": get_final_result(game),
            "terminated": game.terminated, "truncated": game.truncated,
        }
        _log(f"[{side}] FINISHED at turn {game.turn}: {results[side]['final_result']}")
    except Exception as e:  # noqa: BLE001
        import traceback
        errors[side] = f"{e!r}\n{traceback.format_exc()}"
        _log(f"[{side}] ERROR: {e!r}")
    finally:
        try:
            game.close()
        except Exception:  # pragma: no cover
            pass


def orchestrate(cap: int, seed: int, side_a_spec: str, side_b_spec: str,
                client_port: int, out_dir: str, start_delay: float = 5.0,
                join_timeout: float = 600.0) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    _log_lines.clear()

    gameA, gameB = CivRealmGame(), CivRealmGame()
    contA, contB = make_contestant(side_a_spec), make_contestant(side_b_spec)
    query_lock = threading.Lock()
    results: dict = {}
    errors: dict = {}

    _log(f"[orch] cap={cap} seed={seed} port={client_port} | A={side_a_spec}({contA.name()}) "
         f"B={side_b_spec}({contB.name()})")

    tA = threading.Thread(target=_run_side, name="sideA",
                          args=("A", gameA, contA, "agentA", client_port, cap, seed, query_lock,
                                results, errors))
    tB = threading.Thread(target=_run_side, name="sideB",
                          args=("B", gameB, contB, "agentB", client_port, cap, seed, query_lock,
                                results, errors))

    # Start A first so it becomes player 0 and creates the game on the port; then B joins as player 1.
    tA.start()
    time.sleep(start_delay)
    tB.start()

    tA.join(timeout=join_timeout)
    tB.join(timeout=join_timeout)
    hung = [s for s, t in (("A", tA), ("B", tB)) if t.is_alive()]

    # Decide the winner from both sides' final scores, mapped back to the side label.
    verdict = None
    winner_label = None
    if "A" in results and "B" in results:
        fa, fb = results["A"]["final_result"], results["B"]["final_result"]
        verdict = decide_winner(fa, fb)  # winner: 'A' | 'B' | 'tie'
        winner_label = winner_label_for(verdict, side_a_spec, side_b_spec)

    summary = {
        "cap": cap, "seed": seed, "client_port": client_port,
        "side_a": {"spec": side_a_spec, **results.get("A", {})},
        "side_b": {"spec": side_b_spec, **results.get("B", {})},
        "verdict": verdict,
        "winner_side": (verdict or {}).get("winner"),
        "winner_label": winner_label,
        "hung_sides": hung,
        "errors": errors,
        "completed": not hung and not errors and verdict is not None,
    }

    with open(os.path.join(out_dir, "orchestration.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    with open(os.path.join(out_dir, "turnlog.txt"), "w") as f:
        f.write("\n".join(_log_lines))

    _log("\n===== ORCHESTRATION RESULT =====")
    _log(json.dumps({k: summary[k] for k in
                     ["cap", "seed", "side_a", "side_b", "verdict", "winner_label",
                      "hung_sides", "completed"]}, indent=2, default=str))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Two-agent CivRealm orchestration")
    ap.add_argument("--cap", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--side-a", type=str, default="random:1")
    ap.add_argument("--side-b", type=str, default="random:2")
    ap.add_argument("--client-port", type=int, default=6001)
    ap.add_argument("--start-delay", type=float, default=5.0)
    ap.add_argument("--out-dir", type=str, default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(__file__), "runs", "orch-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    summary = orchestrate(args.cap, args.seed, args.side_a, args.side_b,
                          args.client_port, out_dir, start_delay=args.start_delay)
    return 0 if summary.get("completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
