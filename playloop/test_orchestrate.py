"""Unit tests for the orchestrator's winner->side mapping (pure, no env/model).

Run: .venv/bin/python -m playloop.test_orchestrate
"""

from playloop.orchestrate import winner_label_for
from playloop.winrule import decide_winner


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def main() -> int:
    A_SPEC, B_SPEC = "random:1", "local:qwen3:4b"

    # A wins on score -> maps to side A's spec
    a = {"score": 5, "cities": 1, "techs": 1, "population": 1}
    b = {"score": 2, "cities": 9, "techs": 9, "population": 9}
    v = decide_winner(a, b)
    _check("A>B winner is A", v["winner"] == "A")
    _check("winner label -> side A spec", winner_label_for(v, A_SPEC, B_SPEC) == A_SPEC)

    # B wins -> maps to side B's spec (mapping is not transposed)
    v = decide_winner(b, a)
    _check("B>A winner is B", v["winner"] == "B")
    _check("winner label -> side B spec", winner_label_for(v, A_SPEC, B_SPEC) == B_SPEC)

    # Full tie -> "tie" label, no crash
    t = {"score": 0, "cities": 0, "techs": 0, "population": 0, "units": 5}
    v = decide_winner(dict(t), dict(t))
    _check("full tie winner is tie", v["winner"] == "tie")
    _check("tie label contains 'tie'", "tie" in winner_label_for(v, A_SPEC, B_SPEC))

    # The real-game case: 0-0 score resolved by the techs tiebreak (as orch-test1 produced)
    a3 = {"score": 0, "techs": 1, "cities": 0, "population": 0, "units": 5}
    b3 = {"score": 0, "techs": 0, "cities": 0, "population": 0, "units": 0}
    v = decide_winner(a3, b3)
    _check("0-0 score -> techs tiebreak -> A", v["winner"] == "A" and v["by"] == "techs")
    _check("tiebreak winner maps to side A", winner_label_for(v, A_SPEC, B_SPEC) == A_SPEC)

    print("ALL ORCHESTRATE MAPPING TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
