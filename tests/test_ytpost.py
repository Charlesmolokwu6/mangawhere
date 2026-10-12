import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from scrapers import ytpost
from server import db

POST_ID = "UgkxtVMQ2mQLMiYqpVAxXx--1LGtb5DjC0w3"
LIST_TEXT = "1. Chronicles of Runes\n2. L.A.G\n3. Hand Jumper\n4. Surviving the Game as a Barbarian \n"


def post_page(text, author="Haiden"):
    data = {"contents": {"x": [{"backstagePostRenderer": {
        "contentText": {"runs": [{"text": text}]},
        "authorText": {"runs": [{"text": author}]},
    }}]}}
    return ('<html><meta property="og:description" content="cut off...">'
            f"<script>var ytInitialData = {json.dumps(data)};</script></html>")


class PostIdTests(unittest.TestCase):
    def test_post_links_in_both_forms(self):
        self.assertEqual(ytpost.post_id_from_url(
            f"http://youtube.com/post/{POST_ID}?surface=shorts&data=AaEr&si=aQi"), POST_ID)
        self.assertEqual(ytpost.post_id_from_url(
            f"https://www.youtube.com/channel/UCabc/community?lb={POST_ID}"), POST_ID)

    def test_anything_else_is_refused(self):
        for url in (f"https://evil.example/post/{POST_ID}",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/post/bad/../id",
                    "https://www.youtube.com/post/x"):
            self.assertIsNone(ytpost.post_id_from_url(url), url)


class ParseTests(unittest.TestCase):
    def test_full_text_comes_from_the_page_data_not_the_cut_off_meta(self):
        post = ytpost.parse_post_html(post_page(LIST_TEXT))
        self.assertEqual(post, {"text": LIST_TEXT, "author": "Haiden", "images": []})

    def test_meta_description_is_the_fallback(self):
        html = '<meta property="og:description" content="Read &quot;Solo Leveling&quot; now">'
        self.assertEqual(ytpost.parse_post_html(html)["text"], 'Read "Solo Leveling" now')

    def test_numbered_and_bulleted_lists(self):
        self.assertEqual(ytpost.list_titles(LIST_TEXT),
                         ["Chronicles of Runes", "L.A.G", "Hand Jumper", "Surviving the Game as a Barbarian"])
        self.assertEqual(ytpost.list_titles("Top picks:\n- Omniscient Reader\n• Eleceed\n- eleceed"),
                         ["Omniscient Reader", "Eleceed"])

    def test_a_single_line_is_not_a_list(self):
        self.assertEqual(ytpost.list_titles("1. Solo Leveling is back this week!"), [])
        self.assertEqual(ytpost.list_titles("Just a caption about Solo Leveling"), [])


PIC = "https://yt3.ggpht.com/y4gfntw-LbV-Uz3C0GqrF3WIzLFuEY2AgJ3ePT1pxpeFPYNarqUwTGojuZPlUSU5M3Evu7gZ58-MClE"


def picture_post(text, pictures):
    images = [{"backstageImageRenderer": {"image": {"thumbnails": [
        {"url": p + "=s288-c-fcrop64=1,0-nd-v1"}, {"url": p + "=s976-c-fcrop64=1,0-nd-v1"}]}}} for p in pictures]
    data = {"contents": {"x": [{"backstagePostRenderer": {
        "contentText": {"runs": [{"text": text}]},
        "authorText": {"runs": [{"text": "Haiden"}]},
        "backstageAttachment": {"postMultiImageRenderer": {"images": images}},
    }}]}}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def box(height):
    return [[0, 0], [100, 0], [100, height], [0, height]]


class PictureTests(unittest.TestCase):
    def test_each_picture_once_without_its_size(self):
        post = ytpost.parse_post_html(picture_post("Manhwa recommendation - Apocalyptic", [PIC, PIC + "x", PIC]))
        self.assertEqual(post["text"], "Manhwa recommendation - Apocalyptic")
        self.assertEqual(post["images"], [PIC, PIC + "x"])

    def test_only_youtube_pictures_are_read(self):
        self.assertEqual(ytpost.image_base(PIC + "=s480"), PIC)
        for url in ("http://yt3.ggpht.com/abcdefghijklmnopqrstuvwxyz", "https://evil.example/" + "a" * 30,
                    "https://yt3.ggpht.com/../../etc/passwd", "https://yt3.ggpht.com/short", ""):
            self.assertIsNone(ytpost.image_base(url), url)

    def test_the_biggest_lettering_comes_first_without_chapter_counts(self):
        results = [
            [box(18), "95chapters|ongoing", 0.95],
            [box(24), "SURVIVING-THEAPOCALYPSE", 0.97],
            [box(12), "Chapters", 0.95],
            [box(8), "33", 0.99],
            [box(30), "blurry", 0.4],
            [box(21), "335KM", 0.99],
        ]
        self.assertEqual(ytpost.title_lines(results), ["surviving the apocalypse", "335km"])


class PostLookupApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_returns_the_titles_and_fetches_only_by_id(self):
        result = {"text": LIST_TEXT, "author": "Haiden", "titles": ytpost.list_titles(LIST_TEXT)}
        with patch("scrapers.ytpost.lookup_post", AsyncMock(return_value=result)) as lookup:
            r = self.client.get("/api/post-lookup", params={"url": f"https://youtube.com/post/{POST_ID}?si=x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["titles"][1], "L.A.G")
        lookup.assert_awaited_once_with(POST_ID)

    def test_a_picture_title_is_read_and_only_youtube_pictures_are_accepted(self):
        with patch("scrapers.ytpost.title_from_image", AsyncMock(return_value={"title": "Distant Sky", "id": 85879})):
            r = self.client.get("/api/post-image-title", params={"url": PIC})
        self.assertEqual(r.json(), {"title": "Distant Sky", "id": 85879})
        r = self.client.get("/api/post-image-title", params={"url": "https://example.com/" + "a" * 30})
        self.assertEqual(r.status_code, 400)

    def test_a_post_with_only_pictures_is_not_empty(self):
        result = {"text": "", "author": "Haiden", "titles": [], "images": [PIC]}
        with patch("scrapers.ytpost.lookup_post", AsyncMock(return_value=result)):
            r = self.client.get("/api/post-lookup", params={"url": f"https://youtube.com/post/{POST_ID}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["images"], [PIC])

    def test_non_post_links_are_rejected(self):
        r = self.client.get("/api/post-lookup", params={"url": "https://www.youtube.com/watch?v=abc"})
        self.assertEqual(r.status_code, 400)

    def test_an_empty_post_is_a_404_and_a_failed_fetch_a_502(self):
        with patch("scrapers.ytpost.lookup_post", AsyncMock(return_value={"text": "", "author": "", "titles": []})):
            self.assertEqual(self.client.get("/api/post-lookup",
                             params={"url": f"https://youtube.com/post/{POST_ID}"}).status_code, 404)
        with patch("scrapers.ytpost.lookup_post", AsyncMock(side_effect=RuntimeError("blocked"))):
            self.assertEqual(self.client.get("/api/post-lookup",
                             params={"url": f"https://youtube.com/post/{POST_ID}"}).status_code, 502)


if __name__ == "__main__":
    unittest.main()
