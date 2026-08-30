"""
Regression test for the qwen3:4b / gemma4:e4b "empty content" bug — zero model usage.

Bug: both 4B gameplay models are *thinking* models. Left with thinking on (Ollama's default),
they spend the entire num_predict budget on the hidden `message.thinking` channel and return an
EMPTY `message.content` (done_reason=length). Every actor was then skipped and the loop logged
"ollama: no JSON parsed". Fix: OllamaContestant sends `think: False` in the chat payload so the
model emits the JSON answer directly.

This reproduces the symptom against a mocked Ollama endpoint:
  - the payload MUST carry think=False (the actual fix),
  - a thinking-only reply (empty content) reproduces the original failure, and
  - with thinking disabled the model returns content and choices parse.

Collected by `pytest`; also runnable standalone:
    .venv/bin/python -m playloop.test_ollama_think
"""

import contextlib
import sys
import types

from playloop.contestant import OllamaContestant


class _FakeResp:
    def __init__(self, message):
        self._message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": self._message, "done_reason": "stop"}


_SCHEMA = {
    "type": "object",
    "properties": {"choices": {"type": "object",
                               "properties": {"unit:101": {"type": "integer", "enum": [0, 1, 2]}},
                               "required": ["unit:101"], "additionalProperties": False}},
    "required": ["choices"], "additionalProperties": False,
}


@contextlib.contextmanager
def _fake_requests(handler):
    """Swap in a `requests` module whose post() runs `handler(payload)`, then PUT BACK whatever
    was there. Restoring matters under pytest: these modules share one interpreter, and a stray
    fake `requests` would silently follow us into every later test in the run."""
    captured = {}

    def post(url, json=None, timeout=None):
        captured["payload"] = json
        captured["url"] = url
        return _FakeResp(handler(json))

    fake = types.ModuleType("requests")
    fake.post = post
    had = "requests" in sys.modules
    prev = sys.modules.get("requests")
    sys.modules["requests"] = fake
    try:
        yield captured
    finally:
        if had:
            sys.modules["requests"] = prev
        else:
            del sys.modules["requests"]


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def test_think_disabled_and_schema_passed_as_format():
    # The fix: payload disables thinking, and a normal content reply parses to choices.
    handler = lambda payload: {"content": '{"plan":"go","choices":{"unit:101":1}}', "thinking": ""}  # noqa: E731
    with _fake_requests(handler) as cap:
        res = OllamaContestant(model="qwen3:4b").choose_constrained("MENU", schema=_SCHEMA)
    _check("payload sets think=False (the fix)", cap["payload"].get("think") is False)
    # This is the DECODER-level enforcement: the schema goes into Ollama's `format`, so
    # llama.cpp compiles it to a grammar and an out-of-enum option cannot be sampled.
    _check("payload passes the schema through as format", cap["payload"].get("format") == _SCHEMA)
    _check("choices parsed when content present", res.choices == {"unit:101": 1})
    _check("no error on good reply", res.error is None)


def test_empty_content_reproduces_the_bug():
    # Original bug symptom: thinking-only reply (empty content) -> no JSON parsed.
    handler = lambda payload: {"content": "", "thinking": "thinking for a long time..."}  # noqa: E731
    with _fake_requests(handler):
        res = OllamaContestant(model="qwen3:4b").choose_constrained("MENU", schema=_SCHEMA)
    _check("empty content reproduces 'no JSON parsed'", res.error == "ollama: no JSON parsed")
    _check("no choices applied on empty content", res.choices == {})


def test_think_is_configurable():
    # think is configurable (so a future thinking-on experiment is still possible).
    with _fake_requests(lambda payload: {"content": "{}", "thinking": ""}) as cap:
        OllamaContestant(model="qwen3:4b", think=True).choose_constrained("MENU", schema=_SCHEMA)
    _check("think=True is honored when explicitly set", cap["payload"].get("think") is True)


def test_fake_requests_is_restored():
    """The context manager must leave sys.modules exactly as it found it."""
    before = sys.modules.get("requests", "<absent>")
    with _fake_requests(lambda payload: {"content": "{}", "thinking": ""}):
        pass
    _check("sys.modules['requests'] restored", sys.modules.get("requests", "<absent>") is before)


def main() -> int:
    """Standalone runner, so `python -m playloop.test_ollama_think` still works."""
    test_think_disabled_and_schema_passed_as_format()
    test_empty_content_reproduces_the_bug()
    test_think_is_configurable()
    test_fake_requests_is_restored()
    print("ALL OLLAMA THINK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
