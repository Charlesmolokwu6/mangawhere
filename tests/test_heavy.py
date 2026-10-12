import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server import heavy, storyteller


class HeavyTests(unittest.TestCase):
    def test_releasing_ocr_unloads_the_engine(self):
        storyteller._ocr_engine = object()
        heavy.release_ocr()
        self.assertIsNone(storyteller._ocr_engine)
        heavy.release_ocr()   # nothing loaded: nothing to do

    def test_browsers_take_turns_and_unload_ocr_first(self):
        from scrapers import core

        running = {"now": 0, "most": 0}

        class FakePage:
            async def goto(self, *a, **k):
                running["now"] += 1
                running["most"] = max(running["most"], running["now"])
                await asyncio.sleep(0.01)
                running["now"] -= 1

            async def evaluate(self, *a): return None
            async def wait_for_timeout(self, *a): return None
            async def content(self): return "<html></html>"

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=MagicMock(new_page=AsyncMock(return_value=FakePage())))
        browser.close = AsyncMock()
        pw = MagicMock()
        pw.chromium.launch = AsyncMock(return_value=browser)

        class FakePlaywright:
            async def __aenter__(self): return pw
            async def __aexit__(self, *a): return False

        storyteller._ocr_engine = object()

        async def main():
            await asyncio.gather(*[core.fetch_html_playwright(f"https://x{i}.example/") for i in range(4)])

        with patch.object(core, "async_playwright", lambda: FakePlaywright()):
            asyncio.run(main())
        self.assertEqual(running["most"], 1)
        self.assertEqual(browser.close.await_count, 4)
        self.assertIsNone(storyteller._ocr_engine)


if __name__ == "__main__":
    unittest.main()
