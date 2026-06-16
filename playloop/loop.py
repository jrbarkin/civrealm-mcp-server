"""
Headless single-agent play loop over CivRealm.

Plays ONE game vs the built-in AI by looping:
    observe -> shape state + legal actions -> ask the contestant for action(s) -> apply -> end turn,
until a turn cap (default 50) or game end.

Two contestant modes:
  - constrained (default): the contestant picks an option NUMBER from a per-actor menu of that
    actor's legal actions, so an illegal action is impossible by construction (Part A).
  - freeform: the contestant returns free-form action strings (kept for comparison).

At game end the score-based win rule's final result is read and logged (Part B).

Per-turn metrics -> <out>/metrics.jsonl, full transcript -> <out>/transcript.json,
aggregate -> <out>/summary.json. Drives the same env layer as the MCP server (civrealm_mcp.core).

Usage:
    .venv/bin/python -m playloop.loop --max-turns 50 [--mode constrained|freeform]
                                      [--model claude-sonnet-4-6] [--client-port 6001]
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
from playloop.contestant import ClaudeSubagentContestant, OllamaContestant
from playloop.representation import (build_choice_menu, menu_to_schema, raw_snapshot, render,
                                     render_menu, shape)
from playloop.winrule import DEFAULT_PRIMARY, DEFAULT_TIEBREAK, get_final_result


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalize_action(game: CivRealmGame, ctrl_type: str, actor_id: str, action: str) -> str:
    """Map a model's free-form action onto a legal key when it used an alias / spacing variant."""
    if game.is_legal(ctrl_type, actor_id, action):
        return action
    for c in (COMPASS_TO_GOTO.get(action), action.replace(" ", "_"), action.replace("_", " ")):
        if c and game.is_legal(ctrl_type, actor_id, c):
            return c
    return action


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


def invalid_selections(menu, choices: dict) -> dict:
    """PURE. Return {actor_key: info} for every choice that can't be applied — the basis for the
    error feedback sent back to the contestant. Option 0 (skip) and any listed number are valid."""
    bad: dict[str, dict] = {}
    for ak, opt in (choices or {}).items():
        if ak not in menu.index:
            bad[ak] = {"got": opt, "reason": "unknown actor id"}
            continue
        try:
            oi = int(opt)
        except (TypeError, ValueError):
            bad[ak] = {"got": opt, "reason": "not a number", "valid_options": sorted(menu.index[ak])}
            continue
        if oi not in menu.index[ak]:
            bad[ak] = {"got": opt, "reason": "option out of range", "valid_options": sorted(menu.index[ak])}
    return bad


def build_feedback(menu_text: str, bad: dict, valid_actors: list[str]) -> str:
    """PURE. Re-send the menu plus an error message enumerating the acceptable options."""
    lines = [menu_text, "",
             "Your previous answer had INVALID selections. Resend the full choices JSON, fixing these:"]
    for ak, info in bad.items():
        if "valid_options" in info:
            lines.append(f"- {ak}: you chose {info['got']!r} ({info['reason']}); "
                         f"valid option numbers for {ak} are {info['valid_options']}.")
        else:
            sample = ", ".join(valid_actors[:6])
            lines.append(f"- {ak}: {info['reason']} — not one of your actors. "
                         f"Use actor ids exactly as listed (e.g. {sample}).")
    return "\n".join(lines)


def resolve_choices(contestant, menu, menu_text: str, max_retries: int = 2,
                    schema=None) -> tuple[dict, dict]:
    """Ask the contestant for option-number choices; if any are invalid, send feedback that
    enumerates the valid options and retry (merging only the corrected actors). Returns
    (choices, meta) where meta carries plan/error/ms/cost and the retry count.

    `schema` (per-turn JSON Schema from menu_to_schema) is passed to the contestant for
    structured-output enforcement; with it, retries should be rare (invalid output can't be
    emitted), but the retry path remains as a backstop for contestants that ignore the schema."""
    res = contestant.choose_constrained(menu_text, schema=schema)
    choices = dict(res.choices or {})
    meta = {"plan": res.plan, "error": res.error, "ms": res.duration_ms or 0,
            "cost": res.cost_usd or 0, "retries": 0}
    valid_actors = list(menu.index.keys())
    for _ in range(max_retries):
        bad = invalid_selections(menu, choices)
        if not bad:
            break
        meta["retries"] += 1
        r = contestant.choose_constrained(build_feedback(menu_text, bad, valid_actors), schema=schema)
        meta["ms"] += r.duration_ms or 0
        meta["cost"] += r.cost_usd or 0
        if r.error:
            meta["error"] = r.error
        if not r.choices:
            break
        for ak in bad:  # accept corrections only for the actors that were invalid
            if ak in r.choices:
                choices[ak] = r.choices[ak]
    return choices, meta


def apply_freeform(game: CivRealmGame, choice) -> dict:
    acted: set[tuple[str, str]] = set()
    c: Counter = Counter()
    per_move: list[dict] = []
    fam: Counter = Counter()
    run_error = None
    moves = choice.moves or []
    for m in moves:
        if game.done:
            break
        if not isinstance(m, dict) or not m.get("ctrl_type") or m.get("actor_id") is None or not m.get("action"):
            c["malformed"] += 1
            per_move.append({"move": m, "outcome": "malformed"})
            continue
        ct, aid, act = m["ctrl_type"], str(m["actor_id"]), str(m["action"])
        if (ct, aid) in acted:
            c["duplicate"] += 1
            per_move.append({"move": m, "outcome": "duplicate_actor"})
            continue
        nact = normalize_action(game, ct, aid, act)
        if game.is_legal(ct, aid, nact):
            try:
                game.take_action(ct, aid, nact)
            except Exception as e:
                run_error = f"env error on take_action {(ct, aid, nact)}: {e!r}"
                per_move.append({"move": m, "outcome": "env_error", "error": repr(e)})
                break
            c["applied"] += 1
            acted.add((ct, aid))
            fam[action_family(nact)] += 1
            per_move.append({"move": m, "resolved": nact, "outcome": "applied"})
        else:
            c["illegal"] += 1
            per_move.append({"move": m, "resolved": nact, "outcome": "illegal",
                             "valid_sample": game.legal_actions_for(ct, aid)[:8]})
    return {"attempted": len(moves), "applied": c["applied"], "illegal": c["illegal"],
            "malformed": c["malformed"], "duplicate": c["duplicate"], "skipped": 0,
            "stale": 0, "bad_index": 0, "unknown_actor": 0, "per_move": per_move, "families": fam,
            "run_error": run_error, "parsed": moves}


def apply_constrained(game: CivRealmGame, menu, choices: dict) -> dict:
    """Apply the contestant's (already retry-resolved) option-number choices. Illegal actions are
    impossible by construction; residual bad_index/unknown_actor (after retries) are tracked, and a
    rare cross-actor staleness is counted as `stale` (benign, skipped)."""
    acted: set[tuple[str, str]] = set()
    c: Counter = Counter()
    per_move: list[dict] = []
    fam: Counter = Counter()
    run_error = None
    choices = choices or {}
    attempted = 0
    for actor_key, opt in choices.items():
        if game.done:
            break
        if actor_key not in menu.index:
            c["unknown_actor"] += 1
            per_move.append({"actor": actor_key, "opt": opt, "outcome": "unknown_actor"})
            continue
        try:
            opt_i = int(opt)
        except (TypeError, ValueError):
            c["bad_index"] += 1
            per_move.append({"actor": actor_key, "opt": opt, "outcome": "bad_index"})
            continue
        if opt_i == 0:
            c["skipped"] += 1
            per_move.append({"actor": actor_key, "opt": 0, "outcome": "skip"})
            continue
        if opt_i not in menu.index[actor_key]:
            c["bad_index"] += 1
            per_move.append({"actor": actor_key, "opt": opt_i, "outcome": "bad_index"})
            continue
        attempted += 1
        ct, aid, act = menu.index[actor_key][opt_i]
        if (ct, str(aid)) in acted:
            c["duplicate"] += 1
            per_move.append({"actor": actor_key, "opt": opt_i, "outcome": "duplicate_actor"})
            continue
        if game.is_legal(ct, aid, act):
            try:
                game.take_action(ct, aid, act)
            except Exception as e:
                run_error = f"env error on take_action {(ct, aid, act)}: {e!r}"
                per_move.append({"actor": actor_key, "action": act, "outcome": "env_error", "error": repr(e)})
                break
            c["applied"] += 1
            acted.add((ct, str(aid)))
            fam[action_family(act)] += 1
            per_move.append({"actor": actor_key, "opt": opt_i, "action": act, "outcome": "applied"})
        else:
            # Legal when the menu was built, but an earlier same-turn action changed it (e.g.
            # founding a city under stacked units changes the co-located units' legal set). This is
            # a benign sequencing artifact, NOT an illegal proposal — the model picked a then-legal
            # option. Count it separately and skip it, so `illegal` stays 0 by construction.
            c["stale"] += 1
            per_move.append({"actor": actor_key, "opt": opt_i, "action": act, "outcome": "stale_skipped"})
    return {"attempted": attempted, "applied": c["applied"], "illegal": 0, "stale": c["stale"],
            "malformed": 0, "duplicate": c["duplicate"], "skipped": c["skipped"],
            "bad_index": c["bad_index"], "unknown_actor": c["unknown_actor"],
            "per_move": per_move, "families": fam, "run_error": run_error, "parsed": choices}


def play(max_turns: int, client_port: int, username: str, model: str, out_dir: str,
         mode: str = "constrained", primary_metric: str = DEFAULT_PRIMARY,
         backend: str = "claude", seed: int | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    metrics_path = os.path.join(out_dir, "metrics.jsonl")
    transcript_path = os.path.join(out_dir, "transcript.json")
    summary_path = os.path.join(out_dir, "summary.json")

    contestant = (OllamaContestant(model=model) if backend == "local"
                  else ClaudeSubagentContestant(model=model))
    game = CivRealmGame()
    _log(f"[loop] start: backend={backend} contestant={contestant.name()} "
         f"port={client_port} max_turns={max_turns} mode={mode}")
    game.start(client_port=client_port, max_turns=max_turns, username=username, seed=seed)
    _log(f"[loop] reset done. my_player_id={game.my_player_id()} turn={game.turn} seed={seed}")

    metrics: list[dict] = []
    transcript: list[dict] = []
    totals: Counter = Counter()
    applied_family_totals: Counter = Counter()
    run_error = None
    first_turn = game.turn

    mf = open(metrics_path, "w")
    try:
        while not game.done:
            turn = game.turn
            if turn is None or turn > max_turns:
                break

            shaped = shape(game)
            actors_offered = sum(len(a) for a in shaped.actionable.values())
            actions_offered = shaped.num_legal_actions()

            if mode == "constrained":
                menu = build_choice_menu(game)
                state_text = render_menu(menu)
                schema = menu_to_schema(menu)  # structured-output enforcement (enum per actor)
                choices, meta = resolve_choices(contestant, menu, state_text, schema=schema)
                ap = apply_constrained(game, menu, choices)
            else:
                state_text = render(shaped)
                choice = contestant.choose(state_text)
                ap = apply_freeform(game, choice)
                meta = {"plan": choice.plan, "error": choice.error,
                        "ms": choice.duration_ms, "cost": choice.cost_usd, "retries": 0}

            run_error = ap["run_error"]
            if not game.done and run_error is None:
                try:
                    game.end_turn()
                except Exception as e:
                    run_error = f"env error on end_turn (turn {turn}): {e!r}"

            decisions = ap["applied"] + ap["illegal"] + ap["malformed"]
            illegal_rate = (ap["illegal"] + ap["malformed"]) / decisions if decisions else 0.0
            selection_errors = ap["bad_index"] + ap["unknown_actor"]
            row = {
                "turn": turn, "mode": mode,
                "score": shaped.summary.get("score"),
                "techs": shaped.summary.get("techs_researched"),
                "num_units": shaped.summary.get("num_units"),
                "num_cities": shaped.summary.get("num_cities"),
                "actors_offered": actors_offered, "actions_offered": actions_offered,
                "attempted": ap["attempted"], "applied": ap["applied"],
                "illegal": ap["illegal"], "malformed": ap["malformed"], "duplicate": ap["duplicate"],
                "skipped": ap["skipped"], "stale": ap.get("stale", 0),
                "bad_index": ap["bad_index"], "unknown_actor": ap["unknown_actor"],
                "illegal_rate": round(illegal_rate, 3), "selection_errors": selection_errors,
                "applied_families": dict(ap["families"]),
                "plan": meta["plan"], "subagent_error": meta["error"], "retries": meta["retries"],
                "subagent_ms": meta["ms"], "subagent_cost_usd": meta["cost"],
                "reward": game.reward, "terminated": game.terminated, "truncated": game.truncated,
            }
            metrics.append(row)
            mf.write(json.dumps(row) + "\n")
            mf.flush()

            for k in ("attempted", "applied", "illegal", "malformed", "duplicate",
                      "skipped", "stale", "bad_index", "unknown_actor"):
                totals[k] += ap.get(k, 0)
            totals["retries"] += meta["retries"]
            applied_family_totals.update(ap["families"])

            transcript.append({
                "turn": turn, "mode": mode, "state_text": state_text,
                "subagent": {"plan": meta["plan"], "error": meta["error"], "retries": meta["retries"],
                             "ms": meta["ms"], "cost_usd": meta["cost"]},
                "parsed": ap["parsed"], "per_move": ap["per_move"],
                "raw_snapshot": raw_snapshot(game) if turn == first_turn else None,
            })

            _log(f"[turn {turn}] score={row['score']} units={row['num_units']} cities={row['num_cities']} "
                 f"| offered={actors_offered}a/{actions_offered}act attempted={ap['attempted']} "
                 f"applied={ap['applied']} illegal={ap['illegal']} stale={ap.get('stale', 0)} "
                 f"skip={ap['skipped']} badidx={ap['bad_index']} unkactor={ap['unknown_actor']} "
                 f"retries={meta['retries']} | {dict(ap['families'])}"
                 + (f" | ERR={meta['error']}" if meta['error'] else ""))

            if run_error:
                _log(f"[loop] stopping early: {run_error}")
                break
    finally:
        mf.close()

    decisions_total = totals["applied"] + totals["illegal"] + totals["malformed"]
    illegal_rate_overall = (totals["illegal"] + totals["malformed"]) / decisions_total if decisions_total else 0.0
    last = metrics[-1] if metrics else {}
    final_result = get_final_result(game)

    summary = {
        "model": model, "backend": backend, "contestant": contestant.name(), "mode": mode,
        "turns_played": len(metrics), "max_turns": max_turns,
        "completed_without_crash": run_error is None,
        "run_error": run_error,
        "final_turn": last.get("turn"),
        "final_score": last.get("score"),
        "final_units": last.get("num_units"),
        "final_cities": last.get("num_cities"),
        "terminated": game.terminated, "truncated": game.truncated,
        "game_results": game.game_results(),
        "totals": dict(totals),
        "decisions_total": decisions_total,
        "illegal_action_rate": round(illegal_rate_overall, 3),
        "selection_error_total": totals["bad_index"] + totals["unknown_actor"],
        "applied_action_families": dict(applied_family_totals),
        "demonstrably_playing": {
            "founded_cities": applied_family_totals.get("found_city", 0),
            "unit_moves": applied_family_totals.get("move", 0),
            "research_actions": (applied_family_totals.get("research_tech", 0)
                                 + applied_family_totals.get("set_tech_goal", 0)),
            "city_production_actions": applied_family_totals.get("city_production", 0),
            "terrain_improvements": applied_family_totals.get("terrain_improve", 0),
        },
        # Part B: score-based win rule. final_result is read at the cap and logged here; the pure
        # decide_winner(final_result_A, final_result_B) is exercised by playloop/test_winrule.py.
        "win_rule": {"primary": primary_metric, "tiebreak": list(DEFAULT_TIEBREAK),
                     "note": "higher primary at cap wins; ties broken in tiebreak order"},
        "final_result": final_result,
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
    _log(f"\nmetrics: {metrics_path}\ntranscript: {transcript_path}\nsummary: {summary_path}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless single-agent CivRealm play loop")
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--client-port", type=int, default=6001)
    ap.add_argument("--username", type=str, default="myagent")
    ap.add_argument("--backend", choices=["claude", "local"], default="claude",
                    help="claude = `claude` CLI subagent; local = MLX local model (zero API usage)")
    ap.add_argument("--model", type=str, default=None,
                    help="default: claude-opus-4-8 (claude) or mlx Llama-3.2-1B-Instruct-4bit (local)")
    ap.add_argument("--mode", choices=["constrained", "freeform"], default="constrained")
    ap.add_argument("--primary-metric", type=str, default=DEFAULT_PRIMARY)
    ap.add_argument("--seed", type=int, default=None, help="fix map/game/agent seeds for reproducibility")
    ap.add_argument("--out-dir", type=str, default=None)
    args = ap.parse_args()

    model = args.model or ("llama3.2:1b" if args.backend == "local" else "claude-opus-4-8")
    out_dir = args.out_dir or os.path.join(
        os.path.dirname(__file__), "runs", _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        summary = play(args.max_turns, args.client_port, args.username, model, out_dir,
                       mode=args.mode, primary_metric=args.primary_metric, backend=args.backend,
                       seed=args.seed)
    except Exception:
        _log("[loop] FATAL:\n" + traceback.format_exc())
        return 1
    return 0 if summary.get("completed_without_crash") else 2


if __name__ == "__main__":
    raise SystemExit(main())
