"""
Headless single-agent play loop over CivRealm.

Plays ONE game vs the built-in AI by looping:
    observe -> shape state + legal actions -> ask the contestant (an LLM subagent) for moves
    -> apply the legal ones -> end the turn,
until a turn cap (default 50) or game end.

This is a viability probe: the question is whether ONE LLM, driving through our env + shaping
layer, can play coherently for many turns — not whether it wins. We measure the illegal-action
rate and whether it is demonstrably playing (founding cities, moving units, setting research).

Per-turn metrics -> <out>/metrics.jsonl, full transcript -> <out>/transcript.json,
aggregate -> <out>/summary.json. Drives the same env layer as the MCP server (civrealm_mcp.core).

Usage:
    .venv/bin/python -m playloop.loop --max-turns 50 [--model claude-opus-4-8] [--client-port 6001]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import traceback
from collections import Counter

from civrealm_mcp.core import COMPASS_TO_GOTO, CivRealmGame
from playloop.contestant import ClaudeSubagentContestant
from playloop.representation import raw_snapshot, render, shape


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalize_action(game: CivRealmGame, ctrl_type: str, actor_id: str, action: str) -> str:
    """Map a model's action onto a legal key when it used a readable alias / spacing variant."""
    if game.is_legal(ctrl_type, actor_id, action):
        return action
    candidates = [COMPASS_TO_GOTO.get(action), action.replace(" ", "_"), action.replace("_", " ")]
    for c in candidates:
        if c and game.is_legal(ctrl_type, actor_id, c):
            return c
    return action  # unchanged -> will be judged illegal


def action_family(action: str) -> str:
    a = action or ""
    if a.startswith("goto_"):
        return "move"
    if a == "build_city" or a.startswith("found_city"):
        return "found_city"
    if a.startswith("research_tech"):
        return "research_tech"
    if a.startswith("set_tech_goal"):
        return "set_tech_goal"
    if a.startswith("change_gov"):
        return "change_gov"
    if any(a.startswith(p) for p in ("increase_", "decrease_", "set_sci")):
        return "gov_rates"
    if any(a.startswith(p) for p in ("build_road", "build_railroad", "irrigation", "mine",
                                     "cultivate", "plant", "transform", "fortress", "airbase", "pillage")):
        return "terrain_improve"
    if any(a.startswith(p) for p in ("change_unit_prod", "change_improve_prod", "produce",
                                     "city_buy", "city_work", "city_unwork", "city_sell",
                                     "city_change_specialist")):
        return "city_production"
    if a in ("fortify", "sentry", "keep_activity"):
        return "unit_hold"
    return a.split("_")[0] if a else "unknown"


def play(max_turns: int, client_port: int, username: str, model: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    metrics_path = os.path.join(out_dir, "metrics.jsonl")
    transcript_path = os.path.join(out_dir, "transcript.json")
    summary_path = os.path.join(out_dir, "summary.json")

    contestant = ClaudeSubagentContestant(model=model)
    game = CivRealmGame()
    _log(f"[loop] starting game: port={client_port} max_turns={max_turns} model={model}")
    game.start(client_port=client_port, max_turns=max_turns, username=username)
    _log(f"[loop] reset done. my_player_id={game.my_player_id()} turn={game.turn}")

    metrics: list[dict] = []
    transcript: list[dict] = []
    totals = Counter()
    applied_family_totals: Counter = Counter()
    run_error = None

    mf = open(metrics_path, "w")
    try:
        while not game.done:
            turn = game.turn
            if turn is None or turn > max_turns:
                break

            shaped = shape(game)
            state_text = render(shaped)
            actors_offered = sum(len(a) for a in shaped.actionable.values())
            actions_offered = shaped.num_legal_actions()

            choice = contestant.choose(state_text)

            acted: set[tuple[str, str]] = set()
            applied = illegal = duplicate = malformed = 0
            per_move: list[dict] = []
            fam = Counter()

            for m in (choice.moves or []):
                if not isinstance(m, dict):
                    malformed += 1
                    per_move.append({"move": m, "outcome": "malformed"})
                    continue
                ct = m.get("ctrl_type")
                aid = m.get("actor_id")
                act = m.get("action")
                if not ct or aid is None or not act:
                    malformed += 1
                    per_move.append({"move": m, "outcome": "malformed"})
                    continue
                aid = str(aid)
                key = (ct, aid)
                if key in acted:
                    duplicate += 1
                    per_move.append({"move": m, "outcome": "duplicate_actor"})
                    continue
                nact = normalize_action(game, ct, aid, str(act))
                if game.is_legal(ct, aid, nact):
                    try:
                        game.take_action(ct, aid, nact)
                    except Exception as e:  # an env step blew up
                        per_move.append({"move": m, "outcome": "env_error", "error": repr(e)})
                        run_error = f"env error on take_action {key} {nact}: {e!r}"
                        break
                    applied += 1
                    acted.add(key)
                    fam[action_family(nact)] += 1
                    per_move.append({"move": m, "resolved": nact, "outcome": "applied"})
                    if game.done:
                        break
                else:
                    illegal += 1
                    per_move.append({"move": m, "resolved": nact, "outcome": "illegal",
                                     "valid_sample": game.legal_actions_for(ct, aid)[:8]})

            # End the turn (advance) unless the game already ended mid-application.
            if not game.done and run_error is None:
                try:
                    game.end_turn()
                except Exception as e:
                    run_error = f"env error on end_turn (turn {turn}): {e!r}"

            decisions = applied + illegal + malformed
            illegal_rate = (illegal + malformed) / decisions if decisions else 0.0
            row = {
                "turn": turn,
                "score": shaped.summary.get("score"),
                "num_units": shaped.summary.get("num_units"),
                "num_cities": shaped.summary.get("num_cities"),
                "actors_offered": actors_offered,
                "actions_offered": actions_offered,
                "attempted": len(choice.moves or []),
                "applied": applied,
                "illegal": illegal,
                "malformed": malformed,
                "duplicate": duplicate,
                "illegal_rate": round(illegal_rate, 3),
                "applied_families": dict(fam),
                "plan": choice.plan,
                "subagent_error": choice.error,
                "subagent_ms": choice.duration_ms,
                "subagent_cost_usd": choice.cost_usd,
                "reward": game.reward,
                "terminated": game.terminated,
                "truncated": game.truncated,
            }
            metrics.append(row)
            mf.write(json.dumps(row) + "\n")
            mf.flush()

            totals["attempted"] += row["attempted"]
            totals["applied"] += applied
            totals["illegal"] += illegal
            totals["malformed"] += malformed
            totals["duplicate"] += duplicate
            applied_family_totals.update(fam)

            transcript.append({
                "turn": turn,
                "state_text": state_text,
                "subagent": {"plan": choice.plan, "error": choice.error,
                             "raw": choice.raw, "ms": choice.duration_ms, "cost_usd": choice.cost_usd},
                "parsed_moves": choice.moves,
                "per_move": per_move,
                # raw->shaped debuggable: keep the full raw snapshot for the first turn only (bounded transcript)
                "raw_snapshot": raw_snapshot(game) if turn == metrics[0]["turn"] else None,
            })

            _log(f"[turn {turn}] score={row['score']} units={row['num_units']} cities={row['num_cities']} "
                 f"| offered_actors={actors_offered} attempted={row['attempted']} applied={applied} "
                 f"illegal={illegal} malformed={malformed} dup={duplicate} "
                 f"| families={dict(fam)} | plan={choice.plan[:60]!r}"
                 + (f" | ERR={choice.error}" if choice.error else ""))

            if run_error:
                _log(f"[loop] stopping early: {run_error}")
                break
    finally:
        mf.close()

    decisions_total = totals["applied"] + totals["illegal"] + totals["malformed"]
    illegal_rate_overall = (totals["illegal"] + totals["malformed"]) / decisions_total if decisions_total else 0.0
    last = metrics[-1] if metrics else {}
    results = game.game_results()

    summary = {
        "model": model,
        "turns_played": len(metrics),
        "max_turns": max_turns,
        "completed_without_crash": run_error is None,
        "run_error": run_error,
        "final_turn": last.get("turn"),
        "final_score": last.get("score"),
        "final_units": last.get("num_units"),
        "final_cities": last.get("num_cities"),
        "terminated": game.terminated,
        "truncated": game.truncated,
        "game_results": results,
        "totals": dict(totals),
        "decisions_total": decisions_total,
        "illegal_action_rate": round(illegal_rate_overall, 3),
        "applied_action_families": dict(applied_family_totals),
        "demonstrably_playing": {
            "founded_cities": applied_family_totals.get("found_city", 0),
            "unit_moves": applied_family_totals.get("move", 0),
            "research_actions": applied_family_totals.get("research_tech", 0)
                               + applied_family_totals.get("set_tech_goal", 0),
            "city_production_actions": applied_family_totals.get("city_production", 0),
            "terrain_improvements": applied_family_totals.get("terrain_improve", 0),
        },
        "total_cost_usd": round(sum(r["subagent_cost_usd"] or 0 for r in metrics), 4),
    }

    with open(transcript_path, "w") as f:
        json.dump(transcript, f, indent=2, default=str)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    try:
        game.close()
    except Exception:
        pass

    _log("\n===== RUN SUMMARY =====")
    _log(json.dumps(summary, indent=2, default=str))
    _log(f"\nmetrics:    {metrics_path}")
    _log(f"transcript: {transcript_path}")
    _log(f"summary:    {summary_path}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless single-agent CivRealm play loop")
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--client-port", type=int, default=6001)
    ap.add_argument("--username", type=str, default="myagent")
    ap.add_argument("--model", type=str, default="claude-opus-4-8")
    ap.add_argument("--out-dir", type=str, default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(__file__), "runs",
        _dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    try:
        summary = play(args.max_turns, args.client_port, args.username, args.model, out_dir)
    except Exception:
        _log("[loop] FATAL:\n" + traceback.format_exc())
        return 1
    return 0 if summary.get("completed_without_crash") else 2


if __name__ == "__main__":
    raise SystemExit(main())
