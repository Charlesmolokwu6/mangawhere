import asyncio
import os
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


class OcrRepairTests(unittest.TestCase):
    # Real OCR output from Solo Leveling, Tower of God, Omniscient Reader
    # and The Beginning After the End chapters.
    def test_run_together_words_are_split(self):
        self.assertEqual(
            storyteller.normalise_line("THOSETHINGS ARECURRENTLY ABLETOFLY AROLNDINTHE SKY THEn?"),
            "Those things are currently able to fly around in the sky then?",
        )
        self.assertEqual(
            storyteller.normalise_line("THAT'S NOTHOWA S-RANK HUNTERSHOULDBEHAVE. I'MEMBARRASSEDFOROUR COUNTRY."),
            "That's not how a s-rank hunter should behave. I'm embarrassed for our country.",
        )

    def test_u_misread_as_l_is_corrected_only_to_real_words(self):
        self.assertEqual(
            storyteller.normalise_line("I CAN LNDERSTANDWHY MOST HLNTERS WOLLD WANT TO RLN AWAY"),
            "I can understand why most hunters would want to run away",
        )

    def test_names_are_left_alone_rather_than_shredded(self):
        self.assertEqual(storyteller.normalise_line("LREK MAZINO!?"), "Lrek mazino!?")

    def test_mixed_case_caps_contractions_and_possessives(self):
        self.assertEqual(
            storyteller.normalise_line("I'M DESTINED TO BE EVERY GIRL'S HERO, ARen'T I?"),
            "I'm destined to be every girl's hero, aren't I?",
        )

    def test_system_window_symbols_digits_and_ellipses(self):
        self.assertEqual(
            storyteller.normalise_line("区可B [YOURBODYAWAKESDUETOGREATSHOCK.]"), "Your body awakes due to great shock."
        )
        self.assertEqual(
            storyteller.normalise_line("WITHIN JUST2YEARS THEY CHANGED.."), "Within just 2 years they changed..."
        )

    def test_normal_lettering_is_left_as_is(self):
        self.assertEqual(storyteller.normalise_line("Huh? Something's not right"), "Huh? Something's not right")

    def test_credit_lines_are_not_read_out(self):
        prepared = storyteller.prepare_pages([["Art by Sleepy-C I Adapted by UMI", "Episode 156 Chapter 27", "SANGHA..."]])
        self.assertEqual(prepared[0]["lines"], ["Sangha..."])

    def test_publisher_end_pages_and_status_screens_are_not_read_out(self):
        # Seen on Solo Leveling's last page and a hunter's status window.
        prepared = storyteller.prepare_pages([[
            "Comicby:DISCIPLES(REDICESTUDIO) Original Novel by : Chugong Storyby:h-goon",
            "Headof LanguageQuality LetitiaWells HeadofGraphicsQuality SuyeonLee",
            "Thisworkisprotectedbycopyrightlawsandwe prohibitunauthorizeduse",
            "Injury C Injury A Injury A Injury B Injury C Injury B Injury A Injury C",
            "I SAID NO, NO, NO, NO!",
        ]])
        self.assertEqual(prepared[0]["lines"], ["I said no, no, no, no!"])

    def test_missing_spaces_after_punctuation_are_restored(self):
        self.assertEqual(
            storyteller.normalise_line("THE HUNTERS ASSOCIATION.HOW MAY I ASSIST YOU?"),
            "The hunters association. How may I assist you?",
        )
        self.assertEqual(storyteller.normalise_line("HERE,NOW YOU CARRY MY BAG!"), "Here, now you carry my bag!")

    def test_words_hyphenated_across_lines_are_joined(self):
        self.assertEqual(storyteller.normalise_line("EVERY- ONE, GET BACK!"), "Everyone, get back!")
        self.assertEqual(storyteller.normalise_line("WE MUST VET THEIR CHAR- ACTER."), "We must vet their character.")
        self.assertEqual(storyteller.normalise_line("A WELL-KNOWN NAME"), "A well-known name")


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
                started = await storyteller.start("https://site/ch-1", ["a.jpg", "b.jpg"], "jessica")
                await storyteller._jobs[started["job"]]["_task"]
                return started["job"]

        job = asyncio.run(run())
        state = storyteller.get_job(job)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["mode"], "dialogue")
        self.assertEqual(state["voice"], "jessica")
        self.assertEqual((Path(self._dir.name) / f"{job}.txt").read_text(), "\u201cWe have to go,\u201d a voice says.")
        self.assertTrue(storyteller.audio_path(job).exists())

        # Asking again reuses the finished audio instead of redoing it —
        # even once the in-memory job is gone (e.g. after a restart).
        storyteller._jobs.clear()
        with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)):
            again = asyncio.run(storyteller.start("https://site/ch-1", ["a.jpg", "b.jpg"], "jessica"))
        self.assertEqual(again["state"], "done")
        self.assertEqual(again["job"], job)

    def test_a_chapter_with_only_sound_effects_fails_clearly(self):
        async def run():
            with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)), \
                 patch.object(storyteller, "fetch_image", AsyncMock(return_value=b"img")), \
                 patch.object(storyteller, "ocr_page", return_value=["KRR"]):
                started = await storyteller.start("https://site/ch-2", ["a.jpg"], "fenrir")
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
                started = await storyteller.start("https://site/ch-3", ["a.jpg"], "fenrir")
                await storyteller._jobs[started["job"]]["_task"]
                return started["job"]

        state = storyteller.get_job(asyncio.run(run()))
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["mode"], "dialogue")


class VoiceTests(unittest.TestCase):
    def test_listeners_choose_from_five_kokoro_voices(self):
        self.assertEqual(list(storyteller.VOICES), ["fenrir", "echo", "eric", "emma", "jessica"])
        self.assertEqual(storyteller.DEFAULT_VOICE, "fenrir")
        self.assertEqual(storyteller.VOICES["emma"][0], "bf_emma")

    def test_the_picker_gets_names_and_the_default(self):
        with patch.dict("os.environ", {"NARRATION_ENABLED": "1"}), \
             patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)), \
             patch.object(storyteller, "_tts_available", return_value=True), \
             patch.object(storyteller, "_ocr_installed", return_value=True):
            os.environ.pop("NARRATION_URL", None)
            info = asyncio.run(storyteller.availability())
        self.assertIn({"id": "jessica", "name": "Jessica (female)"}, info["voices"])
        self.assertEqual(info["default_voice"], "fenrir")

    def test_an_unknown_voice_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(storyteller, "NARRATION_DIR", Path(d)), \
             patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)), \
             patch.object(storyteller, "_run", AsyncMock()):
            storyteller._jobs.clear()
            state = asyncio.run(storyteller.start("https://site/ch-9", ["a.jpg"], "christopher"))
        self.assertEqual(state["voice"], "fenrir")

    def test_paragraphs_are_spoken_separately_with_a_pause_and_encoded_as_mp3(self):
        import numpy as np

        class FakeKokoro:
            def __init__(self):
                self.calls = []

            def create(self, text, voice, speed, lang):
                self.calls.append((text, voice))
                return np.full(24000, 0.1, dtype="float32"), 24000  # one second each

        kokoro = FakeKokoro()
        mp3 = storyteller._record(kokoro, "\u201cRun!\u201d a voice shouts.\n\nA sound rings out.", "am_fenrir")
        self.assertEqual(kokoro.calls, [("\u201cRun!\u201d a voice shouts.", "am_fenrir"), ("A sound rings out.", "am_fenrir")])
        self.assertTrue(mp3[:3] == b"ID3" or mp3[0] == 0xFF)  # an MP3 stream
        # ~2.9s of audio (2 x 1s + 2 pauses) at 64kbps is roughly 23KB
        self.assertGreater(len(mp3), 15000)


class WhereNarrationRunsTests(unittest.TestCase):
    def _availability(self, **env):
        with patch.dict("os.environ", env, clear=False):
            for key in ("RENDER", "NARRATION_ENABLED", "NARRATION_URL"):
                if key not in env:
                    os.environ.pop(key, None)
            return asyncio.run(storyteller.availability())

    def test_off_by_default_on_render_so_ocr_cant_crash_the_free_plan(self):
        self.assertEqual(self._availability(RENDER="true"), {"available": False})

    def test_can_be_forced_on_or_off(self):
        with patch.object(storyteller, "storyteller_available", AsyncMock(return_value=False)):
            self.assertTrue(self._availability(RENDER="true", NARRATION_ENABLED="1")["available"])
        self.assertEqual(self._availability(NARRATION_ENABLED="false"), {"available": False})

    def test_narration_url_points_the_reader_at_another_server(self):
        self.assertEqual(
            self._availability(RENDER="true", NARRATION_URL="https://narrator.example.com/"),
            {"available": True, "base": "https://narrator.example.com"},
        )


class NarrateApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main
        from server import cache

        cache.clear_memory()

        self.main = main
        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_config_reports_narration_availability(self):
        avail = {"available": True, "storyteller": False, "voices": [{"id": "fenrir", "name": "Fenrir (male)"}]}
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
                json={"chapter_url": "https://ww3.mangafreak.me/Read1_Gosu_1", "voice": "jessica",
                      "images": ["http://169.254.169.254/latest/meta-data"]},
            )
        self.assertEqual(r.status_code, 200)
        scrape.assert_awaited_once_with("https://ww3.mangafreak.me/Read1_Gosu_1")
        start.assert_awaited_once_with("https://ww3.mangafreak.me/Read1_Gosu_1", chapter["images"], "jessica")

    def test_narrate_refuses_when_narration_lives_on_another_server(self):
        remote = {"available": True, "base": "https://narrator.example.com"}
        with patch.object(storyteller, "availability", AsyncMock(return_value=remote)), \
             patch.object(self.main, "scrape_chapter", AsyncMock()) as scrape:
            r = self.client.post("/api/narrate", json={"chapter_url": "https://ww3.mangafreak.me/Read1_Gosu_1"})
        self.assertEqual(r.status_code, 503)
        scrape.assert_not_awaited()

    def test_narrate_rejects_a_non_url(self):
        with patch.object(storyteller, "availability", AsyncMock(return_value={"available": True})):
            r = self.client.post("/api/narrate", json={"chapter_url": "file:///etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_audio_for_an_unknown_or_unfinished_job_is_404(self):
        self.assertEqual(self.client.get("/api/narrate/" + "0" * 20 + "/audio").status_code, 404)
        self.assertEqual(self.client.get("/api/narrate/nope").status_code, 404)
