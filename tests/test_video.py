import unittest
from unittest.mock import patch

from scrapers import video


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
