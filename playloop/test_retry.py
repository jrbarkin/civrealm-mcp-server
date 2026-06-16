"""
Unit tests for the constrained-mode retry-feedback loop — zero model usage (mock contestant).

Verifies: an invalid selection (bad option number / unknown actor) is fed back to the contestant
with the acceptable options enumerated, and a corrected selection is merged in; valid selections
are preserved; and a selection the contestant never fixes remains flagged.

Run: .venv/bin/python -m playloop.test_retry
"""

from playloop.contestant import ChoiceResult
from playloop.loop import build_feedback, invalid_selections, resolve_choices
from playloop.representation import ChoiceMenu, menu_to_schema


def _menu() -> ChoiceMenu:
    return ChoiceMenu(
        summary_lines=["(summary)"],
        actors=[],
        index={
            "unit:101": {0: None, 1: ("unit", "101", "build_city"), 2: ("unit", "101", "goto_1")},
            "city:110": {0: None, 1: ("city", "110", "produce")},
        },
        dropped_actions={},
    )


class MockContestant:
    """Returns preset choice responses in sequence; records the prompts it was given."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.prompts = []

    def name(self):
        return "mock"

    def choose_constrained(self, text):
        self.prompts.append(text)
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return ChoiceResult(choices=dict(r), cost_usd=0.0, duration_ms=1)


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def main() -> int:
    menu = _menu()

    # invalid_selections is pure: flags bad index + unknown actor; passes valid + skip(0).
    bad = invalid_selections(menu, {"unit:101": 9, "city:110": 1, "ghost:1": 5, "unit:101_skip": 0})
    _check("flags out-of-range index", "unit:101" in bad and bad["unit:101"]["valid_options"] == [0, 1, 2])
    _check("flags unknown actor", "ghost:1" in bad)
    _check("valid selection not flagged", "city:110" not in bad)

    # build_feedback enumerates the acceptable options.
    fb = build_feedback("MENU", bad, list(menu.index.keys()))
    _check("feedback lists valid options for unit:101", "[0, 1, 2]" in fb)
    _check("feedback names the unknown actor", "ghost:1" in fb)

    # Retry loop: 1st response has a bad index + unknown actor; 2nd corrects unit:101.
    m = MockContestant([
        {"unit:101": 9, "city:110": 1, "ghost:1": 5},  # bad index + valid + unknown
        {"unit:101": 2, "ghost:1": 5},                 # correction for unit:101 (ghost still wrong)
    ])
    choices, meta = resolve_choices(m, menu, "MENU", max_retries=2)
    _check("retry happened", meta["retries"] >= 1)
    _check("invalid corrected (unit:101 -> 2)", choices["unit:101"] == 2)
    _check("valid preserved (city:110 -> 1)", choices.get("city:110") == 1)
    _check("uncorrected stays invalid (ghost:1)", "ghost:1" in invalid_selections(menu, choices))
    _check("2nd prompt is feedback (mentions valid options)", "[0, 1, 2]" in m.prompts[1])
    _check("zero cost (local/mock)", (meta["cost"] or 0) == 0)

    # Happy path: all valid first try -> no retries.
    m2 = MockContestant([{"unit:101": 1, "city:110": 0}])
    choices2, meta2 = resolve_choices(m2, menu, "MENU", max_retries=2)
    _check("no retry when all valid", meta2["retries"] == 0 and not invalid_selections(menu, choices2))

    # Structured-output schema: pins actor keys + enum of valid option numbers, all required.
    sch = menu_to_schema(menu)["properties"]["choices"]
    _check("schema: no extra keys", sch["additionalProperties"] is False)
    _check("schema: all actors required", set(sch["required"]) == {"unit:101", "city:110"})
    _check("schema: unit:101 enum = valid options", sch["properties"]["unit:101"]["enum"] == [0, 1, 2])
    _check("schema: city:110 enum = valid options", sch["properties"]["city:110"]["enum"] == [0, 1])

    print("ALL RETRY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
