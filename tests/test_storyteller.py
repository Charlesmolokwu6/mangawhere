import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import db, storyteller


def _line(text, x0, y0, x1, y1, score=0.95):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, score]


class OcrGroupingTests(unittest.TestCase):
    def test_lines_of_one_bubble_merge_and_bubbles_come_back_top_to_bottom(self):
        # Real OCR output from a Gosu page: three bubbles, the second and
        # third each wrapped over several lines.
        results = [
            _line("WE HAVE", 425, 1020, 560, 1045),
            _line("THAT'S...", 345, 450, 480, 475),
            _line("THAT WILL", 140, 905, 300, 930),
            _line("GET US TOO IF", 120, 933, 330, 958),
            _line("WE STAY HERE.", 125, 961, 330, 986),
            _line("TO STAY AWAY", 390, 1048, 600, 1073),
            _line("A LITTLE", 440, 1076, 560, 1101),
            _line("FURTHER.", 425, 1104, 555, 1129),
        ]
        self.assertEqual(
            storyteller.group_into_bubbles(results),
            [
                "THAT'S...",
                "THAT WILL GET US TOO IF WE STAY HERE.",
                "WE HAVE TO STAY AWAY A LITTLE FURTHER.",
            ],
        )

    def test_low_confidence_and_single_character_lines_are_dropped(self):
        results = [_line("HELLO THERE.", 0, 0, 100, 20), _line("x", 0, 30, 10, 50), _line("GARBLE", 0, 60, 90, 80, 0.3)]
        self.assertEqual(storyteller.group_into_bubbles(results), ["HELLO THERE."])


class SoundEffectTests(unittest.TestCase):
    def test_onomatopoeia_is_a_sound_effect(self):
        for text in ["KRR", "WOO SWOO", "KRR WRR WAA WAA", "SH."]:
            self.assertTrue(storyteller.is_sound_effect(text), text)

    def test_short_real_speech_is_not(self):
        for text in ["NO!", "WHAT?", "HEY YOU", "WAIT...", "I AM HERE", "THAT WILL GET US TOO IF WE STAY HERE."]:
            self.assertFalse(storyteller.is_sound_effect(text), text)


class ScriptTests(unittest.TestCase):
    def test_noise_is_dropped_and_effects_are_flagged_not_quoted(self):
        prepared = storyteller.prepare_pages([["53>>3", "KRR WOO", "WHAT THE HECK IS GOING ON OVER THERE?"]])
        self.assertEqual(prepared, [{"lines": ["What the heck is going on over there?"], "effects": True}])

    def test_script_is_every_line_verbatim_with_attributions_and_effect_cues(self):
        pages = [
            ["BLACK TORNADO SKILL!"],
            ["KRR WOO"],
            ["KRR"],  # consecutive effect-only pages get a single cue
            ["THAT'S...", "THAT WILL GET US TOO IF WE STAY HERE.", "WE HAVE TO GO."],
            ["WHAT IS THAT?"],
        ]
        self.assertEqual(
            storyteller.dialogue_script(pages),
            "\u201cBlack tornado skill!\u201d a voice shouts.\n\n"
            "A sound rings out.\n\n"
            "\u201cThat's...\u201d someone mutters. \u201cThat will get us too if we stay here. We have to go.\u201d\n\n"
            "\u201cWhat is that?\u201d a voice asks.",
        )

    def test_the_model_only_picks_deliveries_and_bad_answers_fall_back(self):
        pages = [["WE HAVE TO GO NOW."], ["WHO ARE YOU?"], ["RUN!"]]
        sent = []

        async def fake_post(self, url, json=None, **kwargs):
            sent.append(json)
            response = AsyncMock()
            response.raise_for_status = lambda: None
            # "whispers" is allowed; "explodes" isn't, so line 2 falls back
            # to its punctuation ("asks"); line 3 is missing entirely.
            response.json = lambda: {"message": {"content": '{"1": "whispers", "2": "explodes"}'}}
            return response

        with patch("httpx.AsyncClient.post", fake_post):
            script = asyncio.run(storyteller.storyteller_script(pages))

        self.assertEqual(
            script,
            "\u201cWe have to go now,\u201d a voice whispers.\n\n"
            "\u201cWho are you?\u201d someone asks.\n\n"
            "\u201cRun!\u201d a voice shouts.",
        )
        self.assertEqual(sent[0]["format"], "json")
        self.assertIn('1. "We have to go now."', sent[0]["messages"][1]["content"])

    def test_unparseable_model_output_still_produces_the_script(self):
        async def fake_post(self, url, json=None, **kwargs):
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = lambda: {"message": {"content": "Sure! Here you go: shouts"}}
            return response

        with patch("httpx.AsyncClient.post", fake_post):
            script = asyncio.run(storyteller.storyteller_script([["HELLO THERE."]]))
        self.assertEqual(script, "\u201cHello there,\u201d a voice says.")


class JobTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._patch = patch.object(storyteller, "NARRATION_DIR", Path(self._dir.name))
        self._patch.start()
        storyteller._jobs.clear()

    def tearDown(self):
        self._patch.stop()
        self._dir.cleanup()

    def test_job_ids_that_are_not_ours_are_rejected(self):
        for bad in ["../../etc/passwd", "..", "", "ABCDEF", "0" * 21]:
            self.assertIsNone(storyteller.get_job(bad), bad)

    def test_a_chapter_is_read_scripted_and_recorded(self):
        async def fake_synth(script, voice, output_file):
            output_file.write_bytes(b"ID3fake")

        async def run():
            with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)), \
                 patch.object(storyteller, "fetch_image", AsyncMock(return_value=b"img")), \
                 patch.object(storyteller, "ocr_page", side_effect=[["KRR"], ["WE HAVE TO GO."]]), \
                 patch.object(storyteller, "synthesize", side_effect=fake_synth):
                started = await storyteller.start("https://site/ch-1", ["a.jpg", "b.jpg"], "aria")
                await storyteller._jobs[started["job"]]["_task"]
                return started["job"]

        job = asyncio.run(run())
        state = storyteller.get_job(job)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["mode"], "dialogue")
        self.assertEqual(state["voice"], "aria")
        self.assertEqual((Path(self._dir.name) / f"{job}.txt").read_text(), "\u201cWe have to go,\u201d a voice says.")
        self.assertTrue(storyteller.audio_path(job).exists())

        # Asking again reuses the finished audio instead of redoing it —
        # even once the in-memory job is gone (e.g. after a restart).
        storyteller._jobs.clear()
        with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)):
            again = asyncio.run(storyteller.start("https://site/ch-1", ["a.jpg", "b.jpg"], "aria"))
        self.assertEqual(again["state"], "done")
        self.assertEqual(again["job"], job)

    def test_a_chapter_with_only_sound_effects_fails_clearly(self):
        async def run():
            with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)), \
                 patch.object(storyteller, "fetch_image", AsyncMock(return_value=b"img")), \
                 patch.object(storyteller, "ocr_page", return_value=["KRR"]):
                started = await storyteller.start("https://site/ch-2", ["a.jpg"], "christopher")
                await storyteller._jobs[started["job"]]["_task"]
                return started["job"]

        state = storyteller.get_job(asyncio.run(run()))
        self.assertEqual(state["state"], "error")
        self.assertIn("only sound effects", state["error"])

    def test_storyteller_mode_falls_back_to_dialogue_when_the_model_fails(self):
        async def fake_synth(script, voice, output_file):
            output_file.write_bytes(b"ID3fake")

        async def run():
            with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=True)), \
                 patch.object(storyteller, "fetch_image", AsyncMock(return_value=b"img")), \
                 patch.object(storyteller, "ocr_page", return_value=["HELLO THERE."]), \
                 patch.object(storyteller, "storyteller_script", AsyncMock(side_effect=RuntimeError("ollama down"))), \
                 patch.object(storyteller, "synthesize", side_effect=fake_synth):
                started = await storyteller.start("https://site/ch-3", ["a.jpg"], "christopher")
                await storyteller._jobs[started["job"]]["_task"]
                return started["job"]

        state = storyteller.get_job(asyncio.run(run()))
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["mode"], "dialogue")


class NarrateApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        self.main = main
        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_config_reports_narration_availability(self):
        avail = {"available": True, "storyteller": False, "voices": ["christopher", "aria"]}
        with patch.object(storyteller, "availability", AsyncMock(return_value=avail)):
            r = self.client.get("/api/config")
        self.assertEqual(r.json()["narration"], avail)

    def test_narrate_is_unavailable_without_the_dependencies(self):
        with patch.object(storyteller, "availability", AsyncMock(return_value={"available": False})):
            r = self.client.post("/api/narrate", json={"chapter_url": "https://ww3.mangafreak.me/Read1_Gosu_1"})
        self.assertEqual(r.status_code, 503)

    def test_narrate_scrapes_the_chapter_itself_rather_than_taking_image_urls(self):
        avail = {"available": True}
        chapter = {"images": ["https://images.mangafreak.me/1.jpg", "https://images.mangafreak.me/2.jpg"]}
        with patch.object(storyteller, "availability", AsyncMock(return_value=avail)), \
             patch.object(self.main, "scrape_chapter", AsyncMock(return_value=chapter)) as scrape, \
             patch.object(storyteller, "start", AsyncMock(return_value={"job": "a" * 20, "state": "queued"})) as start:
            r = self.client.post(
                "/api/narrate",
                json={"chapter_url": "https://ww3.mangafreak.me/Read1_Gosu_1", "voice": "aria",
                      "images": ["http://169.254.169.254/latest/meta-data"]},
            )
        self.assertEqual(r.status_code, 200)
        scrape.assert_awaited_once_with("https://ww3.mangafreak.me/Read1_Gosu_1")
        start.assert_awaited_once_with("https://ww3.mangafreak.me/Read1_Gosu_1", chapter["images"], "aria")

    def test_narrate_rejects_a_non_url(self):
        with patch.object(storyteller, "availability", AsyncMock(return_value={"available": True})):
            r = self.client.post("/api/narrate", json={"chapter_url": "file:///etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_audio_for_an_unknown_or_unfinished_job_is_404(self):
        self.assertEqual(self.client.get("/api/narrate/" + "0" * 20 + "/audio").status_code, 404)
        self.assertEqual(self.client.get("/api/narrate/nope").status_code, 404)
