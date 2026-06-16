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

Run: .venv/bin/python -m playloop.test_ollama_think
"""

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


def _install_fake_requests(handler):
    """Replace the `requests` module _chat imports with one whose post() runs `handler(payload)`."""
    captured = {}

    def post(url, json=None, timeout=None):
        captured["payload"] = json
        captured["url"] = url
        return _FakeResp(handler(json))

    fake = types.ModuleType("requests")
    fake.post = post
    sys.modules["requests"] = fake
    return captured


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def main() -> int:
    schema = {
        "type": "object",
        "properties": {"choices": {"type": "object",
                                   "properties": {"unit:101": {"type": "integer", "enum": [0, 1, 2]}},
                                   "required": ["unit:101"], "additionalProperties": False}},
        "required": ["choices"], "additionalProperties": False,
    }

    # 1) The fix: payload disables thinking, and a normal content reply parses to choices.
    cap = _install_fake_requests(
        lambda payload: {"content": '{"plan":"go","choices":{"unit:101":1}}', "thinking": ""})
    c = OllamaContestant(model="qwen3:4b")
    res = c.choose_constrained("MENU", schema=schema)
    _check("payload sets think=False (the fix)", cap["payload"].get("think") is False)
    _check("payload passes the schema through as format", cap["payload"].get("format") == schema)
    _check("choices parsed when content present", res.choices == {"unit:101": 1})
    _check("no error on good reply", res.error is None)

    # 2) Original bug symptom: thinking-only reply (empty content) -> no JSON parsed.
    _install_fake_requests(
        lambda payload: {"content": "", "thinking": "let me think about this for a long time..."})
    res2 = c.choose_constrained("MENU", schema=schema)
    _check("empty content reproduces 'no JSON parsed'", res2.error == "ollama: no JSON parsed")
    _check("no choices applied on empty content", res2.choices == {})

    # 3) think is configurable (so a future thinking-on experiment is still possible).
    cap3 = _install_fake_requests(lambda payload: {"content": "{}", "thinking": ""})
    OllamaContestant(model="qwen3:4b", think=True).choose_constrained("MENU", schema=schema)
    _check("think=True is honored when explicitly set", cap3["payload"].get("think") is True)

    print("ALL OLLAMA THINK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
