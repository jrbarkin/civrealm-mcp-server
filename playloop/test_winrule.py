"""Unit tests for the pure decide_winner (no env needed).

Collected by `pytest`; also runnable standalone: .venv/bin/python -m playloop.test_winrule
"""

from playloop.winrule import decide_winner


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def test_primary_metric_decides():
    # A wins on primary (score)
    a = {"score": 10, "cities": 1, "techs": 1, "population": 1}
    b = {"score": 8, "cities": 9, "techs": 9, "population": 9}
    r = decide_winner(a, b)
    _check("A>B on score", r["winner"] == "A" and r["by"] == "score")

    # B wins on primary (symmetry)
    r = decide_winner(b, a)
    _check("B>A on score (swapped)", r["winner"] == "B" and r["by"] == "score")


def test_tiebreak_order():
    # Tie on score -> first tiebreak (cities)
    a = {"score": 10, "cities": 3, "techs": 1, "population": 1}
    b = {"score": 10, "cities": 2, "techs": 9, "population": 9}
    r = decide_winner(a, b)
    _check("tie score -> cities", r["winner"] == "A" and r["by"] == "cities")

    # Tie on score+cities -> techs
    a = {"score": 5, "cities": 2, "techs": 7, "population": 1}
    b = {"score": 5, "cities": 2, "techs": 3, "population": 9}
    r = decide_winner(a, b)
    _check("tie score,cities -> techs", r["winner"] == "A" and r["by"] == "techs")

    # Tie on score+cities+techs -> population
    a = {"score": 5, "cities": 2, "techs": 3, "population": 9}
    b = {"score": 5, "cities": 2, "techs": 3, "population": 4}
    r = decide_winner(a, b)
    _check("tie score,cities,techs -> population", r["winner"] == "A" and r["by"] == "population")

    # Full tie on every metric -> tie
    a = {"score": 5, "cities": 2, "techs": 3, "population": 4, "units": 6}
    b = dict(a)
    r = decide_winner(a, b)
    _check("full tie -> tie", r["winner"] == "tie" and r["by"] is None)


def test_primary_override_and_missing_keys():
    # Configurable primary metric (cities as primary)
    a = {"score": 1, "cities": 5, "techs": 0, "population": 0}
    b = {"score": 9, "cities": 4, "techs": 0, "population": 0}
    r = decide_winner(a, b, primary="cities")
    _check("primary=cities ignores score", r["winner"] == "A" and r["by"] == "cities")

    # Missing keys default to 0
    r = decide_winner({"score": 1}, {})
    _check("missing keys default 0", r["winner"] == "A" and r["by"] == "score")


def main() -> int:
    """Standalone runner, so `python -m playloop.test_winrule` still works."""
    test_primary_metric_decides()
    test_tiebreak_order()
    test_primary_override_and_missing_keys()
    print("ALL WINRULE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
