import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from server import banners, db


def banner(seed=1, w=800, h=480):
    """A busy block of 'artwork', the same for the same seed."""
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), (20, 20, 30))
    d = ImageDraw.Draw(im)
    for _ in range(60):
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.rectangle([x, y, x + rnd.randrange(20, 200), y + rnd.randrange(10, 120)],
                    fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im


def page(top=None, story_seed=99, w=800, h=1250, gap=120, bottom=None):
    """A page: an optional banner, a blank gap, story art, and an optional
    banner at the bottom."""
    im = Image.new("RGB", (w, h), "white")
    y = 0
    if top is not None:
        im.paste(top, (0, 0))
        y = top.height + gap
    story = banner(story_seed, w, 300)
    im.paste(story, (0, y))
    if bottom is not None:
        im.paste(bottom, (0, h - bottom.height))
    return im


def jpeg(im, quality=85):
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality)
    return out.getvalue()


class EdgeBlockTests(unittest.TestCase):
    def test_finds_the_banner_above_the_first_gap(self):
        self.assertAlmostEqual(banners.edge_block(page(top=banner())), 480, delta=10)

    def test_finds_a_banner_at_the_bottom(self):
        self.assertAlmostEqual(banners.edge_block(page(bottom=banner(3, h=400)), from_bottom=True), 400, delta=10)

    def test_no_block_when_the_page_starts_blank_or_runs_on(self):
        self.assertIsNone(banners.edge_block(Image.new("RGB", (800, 1250), "white")))
        # Story art straight down the page, no gap: not banner-shaped.
        self.assertIsNone(banners.edge_block(banner(5, 800, 1250)))

    def test_recompressed_copies_have_the_same_fingerprint(self):
        a = Image.open(io.BytesIO(jpeg(page(top=banner()), 90)))
        b = Image.open(io.BytesIO(jpeg(page(top=banner(), story_seed=7), 60)))
        fa, fb = banners.fingerprint(a, 480), banners.fingerprint(b, 480)
        self.assertLessEqual(bin(fa ^ fb).count("1"), banners.MATCH_BITS)
        other = banners.fingerprint(page(top=banner(2)), 480)
        self.assertGreater(bin(fa ^ other).count("1"), banners.MATCH_BITS)

    def test_neighbour_chapters_by_number(self):
        self.assertEqual(banners.neighbour_urls("https://comizy.io/x-2/chapter-49")[:4],
                         ["https://comizy.io/x-2/chapter-48", "https://comizy.io/x-2/chapter-50",
                          "https://comizy.io/x-2/chapter-47", "https://comizy.io/x-2/chapter-51"])
        self.assertEqual(banners.neighbour_urls("https://site/manga/abc"), [])


class TrimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()
        banners.reset()
        vortex, lagoon = banner(1), banner(4, h=380)
        self.pages = {
            "https://comizy.io/s/chapter-49": [page(top=vortex, story_seed=49), page(story_seed=490)],
            "https://comizy.io/s/chapter-48": [page(story_seed=48), page(story_seed=480)],  # a different notice
            "https://comizy.io/s/chapter-47": [page(top=vortex, story_seed=47), page(story_seed=470, bottom=lagoon)],
            "https://comizy.io/s/chapter-46": [page(story_seed=46), page(story_seed=460, bottom=lagoon)],
            "https://comizy.io/other/chapter-3": [page(top=vortex, story_seed=3), page(story_seed=30)],
            "https://comizy.io/clean/chapter-9": [page(top=banner(8), story_seed=9), page(story_seed=90)],
        }

    async def scrape(self, url):
        n = len(self.pages.get(url, []))
        return {"images": [f"{url}#{i}" for i in range(n)]}

    async def fetch(self, client, image_url, chapter_url):
        url, i = image_url.split("#")
        return jpeg(self.pages[url][int(i)])

    async def trim(self, url):
        with patch("server.storyteller.fetch_image", self.fetch):
            return await banners.trim_for(url, self.scrape)

    async def test_a_banner_repeated_on_a_nearby_chapter_is_trimmed_and_learned(self):
        r = await self.trim("https://comizy.io/s/chapter-49")
        self.assertAlmostEqual(r["top"].pop("px"), 480, delta=10)
        self.assertEqual(r["top"], {"width": 800, "height": 1250})
        self.assertIsNone(r["bottom"])
        # Learned: another series by the same group, with no neighbours to compare.
        r = await self.trim("https://comizy.io/other/chapter-3")
        self.assertAlmostEqual(r["top"]["px"], 480, delta=10)
        # And it survives a restart.
        banners.reset()
        r = await self.trim("https://comizy.io/other/chapter-3")
        self.assertAlmostEqual(r["top"]["px"], 480, delta=10)

    async def test_bottom_banners_too(self):
        r = await self.trim("https://comizy.io/s/chapter-47")
        self.assertAlmostEqual(r["bottom"].pop("px"), 380, delta=10)
        self.assertEqual(r["bottom"], {"width": 800, "height": 1250})

    async def test_artwork_that_never_repeats_is_left_alone(self):
        r = await self.trim("https://comizy.io/clean/chapter-9")
        self.assertEqual(r, {"top": None, "bottom": None})
