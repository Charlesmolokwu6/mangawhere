import asyncio
import os
import unittest
from unittest.mock import patch

from server import identify


class _Resp:
    def __init__(self, content):
        self._c = content
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class _Client:
    def __init__(self, content):
        self._c = content
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, *a, **k):
        return _Resp(self._c)


def _run(text, content, env=None):
    env = {"LLM_API_KEY": "k", **(env or {})}
    with patch.dict(os.environ, env), \
            patch.object(identify.httpx, "AsyncClient", lambda *a, **k: _Client(content)):
        return asyncio.run(identify.identify_from_plot(text))


class IdentifyTests(unittest.TestCase):
    def test_off_without_a_key(self):
        with patch.dict(os.environ, {"LLM_API_KEY": ""}, clear=False):
            os.environ.pop("LLM_API_KEY", None)
            self.assertFalse(identify.available())
            self.assertIsNone(asyncio.run(identify.identify_from_plot("some plot")))

    def test_a_confident_name_comes_back_clean(self):
        self.assertEqual(_run("a pacifist in a world of fists", "Fight Change"), "Fight Change")
        self.assertEqual(_run("x", '"Solo Leveling"'), "Solo Leveling")  # quotes stripped

    def test_unknown_is_treated_as_no_answer(self):
        self.assertIsNone(_run("something vague", "UNKNOWN"))
        self.assertIsNone(_run("something vague", "UNKNOWN."))

    def test_an_explanation_is_rejected_not_searched(self):
        wordy = "I'm not sure, but this could possibly be a few different manhwa titles honestly"
        self.assertIsNone(_run("plot", wordy))

    def test_empty_text_makes_no_call(self):
        self.assertIsNone(_run("   ", "Fight Change"))


if __name__ == "__main__":
    unittest.main()
