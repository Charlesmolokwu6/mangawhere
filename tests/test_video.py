import os
import unittest
from unittest.mock import patch

from scrapers import video


class CookiesPathTests(unittest.TestCase):
    def setUp(self):
        # _cookies_path() caches its result for the life of the process
        # (the env var can't change without a redeploy) -- reset that cache
        # between tests so each one sees its own patched environment.
        video._cookie_file_loaded = False
        video._cookie_file_path = None

    def tearDown(self):
        if video._cookie_file_path:
            try:
                os.remove(video._cookie_file_path)
            except OSError:
                pass
        video._cookie_file_loaded = False
        video._cookie_file_path = None

    def test_returns_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOUTUBE_COOKIES", None)
            self.assertIsNone(video._cookies_path())

    def test_writes_cookie_content_to_a_private_temp_file(self):
        content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123"
        with patch.dict(os.environ, {"YOUTUBE_COOKIES": content}):
            path = video._cookies_path()
        self.assertIsNotNone(path)
        with open(path) as f:
            self.assertIn("SID\tabc123", f.read())
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")

    def test_result_is_cached_across_calls(self):
        with patch.dict(os.environ, {"YOUTUBE_COOKIES": "cookie-a"}):
            first = video._cookies_path()
        with patch.dict(os.environ, {"YOUTUBE_COOKIES": "cookie-b"}):
            second = video._cookies_path()
        self.assertEqual(first, second)

    def test_extract_metadata_passes_cookiefile_when_configured(self):
        with patch.dict(os.environ, {"YOUTUBE_COOKIES": "cookie-content"}), \
             patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
            mock_ydl.extract_info.return_value = {"title": "t", "description": "d", "duration": 5}
            video._extract_metadata("https://example.com/v")
        opts = mock_ydl_cls.call_args[0][0]
        self.assertEqual(opts["cookiefile"], video._cookie_file_path)


class VideoLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_metadata_retries_once_on_transient_failure(self):
        meta = {"title": "Recovered", "description": "", "duration": 10}
        with patch.object(
            video, "_extract_metadata", side_effect=[RuntimeError("bot check"), meta]
        ) as mock_extract, patch("asyncio.sleep", return_value=None):
            result = await video.fetch_metadata("https://example.com/v")
        self.assertEqual(result, meta)
        self.assertEqual(mock_extract.call_count, 2)

    async def test_fetch_metadata_raises_after_second_failure(self):
        with patch.object(
            video, "_extract_metadata", side_effect=RuntimeError("still blocked")
        ), patch("asyncio.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                await video.fetch_metadata("https://example.com/v")

    async def test_transcribe_audio_skips_long_videos_without_downloading(self):
        with patch.object(video, "_download_audio") as mock_download:
            result = await video.transcribe_audio(
                "https://example.com/v", duration_seconds=video.MAX_TRANSCRIBE_SECONDS + 1
            )
        self.assertIsNone(result)
        mock_download.assert_not_called()

    async def test_transcribe_audio_transcribes_short_videos(self):
        with patch.object(video, "_download_audio", return_value="/tmp/audio.webm"), \
             patch.object(video, "_transcribe", return_value="someone stop her chapter 12"):
            result = await video.transcribe_audio("https://example.com/v", duration_seconds=30)
        self.assertEqual(result, "someone stop her chapter 12")

    async def test_transcribe_audio_returns_none_on_failure(self):
        with patch.object(video, "_download_audio", side_effect=RuntimeError("blocked")):
            result = await video.transcribe_audio("https://example.com/v", duration_seconds=30)
        self.assertIsNone(result)

    async def test_lookup_combines_metadata_and_transcript(self):
        meta = {"title": "Someone Stop Her! chapter 12", "description": "Sauce: Someone Stop Her!", "duration": 20}
        with patch.object(video, "fetch_metadata", return_value=meta), \
             patch.object(video, "transcribe_audio", return_value="someone stop her"):
            result = await video.lookup("https://example.com/v")
        self.assertEqual(result["title"], meta["title"])
        self.assertEqual(result["description"], meta["description"])
        self.assertEqual(result["transcript"], "someone stop her")

    async def test_lookup_survives_metadata_failure(self):
        with patch.object(video, "fetch_metadata", side_effect=RuntimeError("404")), \
             patch.object(video, "transcribe_audio", return_value=None) as mock_transcribe:
            result = await video.lookup("https://example.com/v")
        self.assertEqual(result, {"title": "", "description": "", "transcript": ""})
        mock_transcribe.assert_called_once_with("https://example.com/v", 0)


if __name__ == "__main__":
    unittest.main()
