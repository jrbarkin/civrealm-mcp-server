"""
Unit tests for the constrained-mode retry-feedback loop — zero model usage (mock contestant).

Verifies: an invalid selection (bad option number / unknown actor) is fed back to the contestant
with the acceptable options enumerated, and a corrected selection is merged in; valid selections
are preserved; and a selection the contestant never fixes remains flagged.

Collected by `pytest`; also runnable standalone: .venv/bin/python -m playloop.test_retry
"""

import json

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

    def choose_constrained(self, text, schema=None):
        self.prompts.append(text)
        self.last_schema = schema
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return ChoiceResult(choices=dict(r), cost_usd=0.0, duration_ms=1)


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def test_invalid_selections_and_feedback():
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


def test_retry_loop_merges_only_corrections():
    menu = _menu()

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


def test_menu_to_schema_shape():
    menu = _menu()

    # Structured-output schema: pins actor keys + enum of valid option numbers, all required.
    sch = menu_to_schema(menu)["properties"]["choices"]
    _check("schema: no extra keys", sch["additionalProperties"] is False)
    _check("schema: all actors required", set(sch["required"]) == {"unit:101", "city:110"})
    _check("schema: unit:101 enum = valid options", sch["properties"]["unit:101"]["enum"] == [0, 1, 2])
    _check("schema: city:110 enum = valid options", sch["properties"]["city:110"]["enum"] == [0, 1])


def test_claude_cli_invocation_carries_the_schema():
    """The Claude backend must actually PASS the per-turn schema to the CLI (`--json-schema`),
    not accept and discard it: that flag is what makes the CLI validate the model's answer and
    retry. Regression guard — this used to be a no-op. No model is spawned; subprocess.run is
    stubbed, so this stays offline."""
    import subprocess as _sp

    from playloop import contestant as _c

    schema = menu_to_schema(_menu())
    seen = {}

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"result": "{}", "duration_ms": 5, "total_cost_usd": 0.0,
                             "is_error": False,
                             "structured_output": {"plan": "p", "choices": {"unit:101": 2}}})

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    real_run = _sp.run
    _c.subprocess.run = _fake_run
    try:
        res = _c.ClaudeSubagentContestant().choose_constrained("MENU", schema=schema)
    finally:
        _c.subprocess.run = real_run

    cmd = seen["cmd"]
    _check("--json-schema is passed to the CLI", "--json-schema" in cmd)
    _check("the schema passed is menu_to_schema's",
           json.loads(cmd[cmd.index("--json-schema") + 1]) == schema)
    _check("the CLI's validated structured_output is what gets used",
           res.choices == {"unit:101": 2} and res.plan == "p")


def test_claude_cli_null_structured_output_is_an_error():
    """When the model never produces a schema-conforming answer the CLI returns
    structured_output: null. That must surface as an error (so the loop retries), not as an
    empty-but-successful turn. This is the CLI path's real failure mode, observed live."""
    import subprocess as _sp

    from playloop import contestant as _c

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"result": "I cannot satisfy that schema.", "is_error": False,
                             "duration_ms": 5, "total_cost_usd": 0.0, "structured_output": None})

    real_run = _sp.run
    _c.subprocess.run = lambda cmd, **kw: _Proc()
    try:
        res = _c.ClaudeSubagentContestant().choose_constrained(
            "MENU", schema=menu_to_schema(_menu()))
    finally:
        _c.subprocess.run = real_run

    _check("null structured_output -> error", res.error is not None)
    _check("no choices invented on failure", res.choices == {})


def test_non_object_json_is_rejected_not_crashed():
    """REGRESSION. `_extract_json_object` is annotated `-> dict | None`, but json.loads happily
    returns a list/str/int for a well-formed non-object reply. Callers immediately do
    `parsed.get("choices")`, so a model answering `[{"unit:101": 1}]` — or just `"skip"` —
    used to raise AttributeError and kill the run instead of being treated as an unparseable
    reply and retried."""
    from playloop.contestant import _extract_json_object, _norm_choices, _norm_moves

    for text in ('[{"unit:101": 1}]', '"skip"', '42', 'true', 'null'):
        got = _extract_json_object(text)
        _check(f"non-object reply {text!r} -> None", got is None)

    _check("real objects still parse", _extract_json_object('{"choices": {"unit:101": 1}}')
           == {"choices": {"unit:101": 1}})
    _check("fenced objects still parse",
           _extract_json_object('```json\n{"plan":"p","choices":{}}\n```')
           == {"plan": "p", "choices": {}})
    _check("object after prose still parses",
           _extract_json_object('Here you go: {"choices": {"unit:1": 0}} done')
           == {"choices": {"unit:1": 0}})
    # And the normalizers stay total on whatever comes back.
    _check("_norm_choices tolerates an empty dict", _norm_choices({}) == {})
    _check("_norm_moves tolerates an empty dict", _norm_moves({}) == [])


def test_summary_counts_turns_whose_contestant_failed():
    """REGRESSION. `completed_without_crash` only watches the ENVIRONMENT, so a run whose
    contestant errored on every turn still reports run_error None and a full turn count — which
    is exactly how the credit-exhausted run in runs/ab-opus48-50t/ reports itself as a clean
    50-turn run with 25 dead turns in it. summary.json must carry the count so a reader of the
    artifact alone can tell."""
    import inspect

    from playloop import loop as _loop

    src = inspect.getsource(_loop.play)
    _check("summary carries turns_with_contestant_error", '"turns_with_contestant_error"' in src)
    # and it is derived from the per-turn field that actually records it
    _check("counted from per-turn subagent_error",
           'r.get("subagent_error")' in src)


def main() -> int:
    """Standalone runner, so `python -m playloop.test_retry` still works."""
    test_invalid_selections_and_feedback()
    test_retry_loop_merges_only_corrections()
    test_menu_to_schema_shape()
    test_claude_cli_invocation_carries_the_schema()
    test_claude_cli_null_structured_output_is_an_error()
    test_non_object_json_is_rejected_not_crashed()
    test_summary_counts_turns_whose_contestant_failed()
    print("ALL RETRY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
