import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from server import banners, db


def art(seed=1, w=800, h=480, background=(20, 20, 30)):
    """A busy block of 'artwork', the same for the same seed."""
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), background)
    d = ImageDraw.Draw(im)
    for _ in range(max(60, w * h // 6000)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.rectangle([x, y, x + rnd.randrange(20, 200), y + rnd.randrange(10, 120)],
                    fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im


def page(top=None, story_seed=99, w=800, h=1250, gap=120, bottom=None):
    """A piece of a chapter: an optional banner, a blank gap, story art
    down to the end, and an optional banner at the bottom."""
    im = Image.new("RGB", (w, h), "white")
    y = 0
    if top is not None:
        im.paste(top, (0, 0))
        y = top.height + gap
    end = h - (bottom.height + gap if bottom is not None else 0)
    im.paste(art(story_seed, w, end - y), (0, y))
    if bottom is not None:
        im.paste(bottom, (0, h - bottom.height))
    return im


def sparse(seed, w=800, h=1250):
    """White with a few small marks: speech-bubble text, a '!'."""
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rnd.randrange(w - 40), rnd.randrange(h - 40)
        d.rectangle([x, y, x + rnd.randrange(5, 30), y + rnd.randrange(5, 30)], fill="black")
    return im


def jpeg(im, quality=85):
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality)
    return out.getvalue()


def cut_rows(trim):
    return trim["skip"], trim["px"]


class StripTests(unittest.TestCase):
    def rows(self, *images):
        out = []
        for im in images:
            out += banners.image_rows(Image.open(io.BytesIO(jpeg(im))))
        return out

    def test_a_shared_banner_is_matched_up_to_its_gap(self):
        a = self.rows(page(top=art(1), story_seed=49), page(story_seed=490))
        b = self.rows(page(top=art(1), story_seed=50), page(story_seed=500))
        length = banners.shared_start(a, b)
        # 480px of an 800px-wide image, in bands of 800/64 = 12.5px
        self.assertAlmostEqual(length * 12.5, 480, delta=15)

    def test_the_match_stops_at_the_gap_even_if_the_story_starts_alike(self):
        story = art(7, 800, 30)
        first = page(top=art(1), story_seed=49)
        second = page(top=art(1), story_seed=50)
        first.paste(story, (0, 600))
        second.paste(story, (0, 600))      # one band of story alike, then not
        length = banners.shared_start(self.rows(first, page()), self.rows(second, page()))
        self.assertAlmostEqual(length * 12.5, 480, delta=15)

    def test_different_art_and_sparse_marks_do_not_match(self):
        self.assertEqual(banners.shared_start(self.rows(page(story_seed=1), page()),
                                              self.rows(page(story_seed=2), page())), 0)
        self.assertEqual(banners.shared_start(self.rows(sparse(1), sparse(11)),
                                              self.rows(sparse(2), sparse(12))), 0)

    def test_two_copies_of_one_chapter_are_not_a_banner(self):
        im = [page(story_seed=5), page(story_seed=6)]
        self.assertEqual(banners.shared_start(self.rows(*im), self.rows(*im)), 0)

    def test_a_notice_at_both_ends_is_found_even_resized(self):
        notice = art(21, 1516, 2300)
        head = self.rows(notice.crop((0, 0, 1516, 1536)), self._join(notice.crop((0, 1536, 1516, 2300)), 3))
        # At the end it's cut in other places, and the last piece is smaller.
        tail = self.rows(self._join_end(notice.crop((0, 0, 1516, 900)), 4),
                         notice.crop((0, 900, 1516, 2300)).resize((1400, 1293)))
        length = banners.both_ends(head, tail)
        # 2300px of a 1516px-wide notice, in bands of 1516/64 px
        self.assertAlmostEqual(length * 1516 / 64, 2300, delta=60)
        self.assertEqual(banners.both_ends(self.rows(sparse(1), sparse(2)), self.rows(sparse(3), sparse(4))), 0)

    def _join(self, notice_part, seed):
        """notice_part at the top of a 1516x1536 piece, story below."""
        im = page(story_seed=seed, w=1516, h=1536)
        im.paste(notice_part, (0, 0))
        return im

    def _join_end(self, notice_part, seed):
        """story above, notice_part at the bottom of a 1516x1536 piece."""
        im = page(story_seed=seed, w=1516, h=1536)
        im.paste(notice_part, (0, 1536 - notice_part.height))
        return im

    def test_cuts_map_back_to_images(self):
        segments = [(0, 0, 100, 800, 1250), (1, 100, 100, 800, 1250)]
        self.assertEqual(banners._cut(segments, 40), {"skip": 0, "px": 500, "width": 800, "height": 1250})
        self.assertEqual(banners._cut(segments, 150), {"skip": 1, "px": 625, "width": 800, "height": 1250})
        self.assertEqual(banners._cut(segments, 100), {"skip": 1, "px": 0, "width": 800, "height": 1250})
        # A credits page wider than what follows goes whole, however much matched.
        credits = [(0, 0, 57, 1200, 1076), (1, 57, 80, 720, 900)]
        self.assertEqual(banners._cut(credits, 10), {"skip": 1, "px": 0, "width": 720, "height": 900})
        self.assertIsNone(banners._cut(segments, 0))

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
        vortex, lagoon = art(1), art(4, h=380)
        self.pages = {}

        def chapter(url, first, last, middle=2):
            self.pages[url] = [first] + [page(story_seed=hash((url, i)) % 1000) for i in range(middle)] + [last]

        chapter("https://comizy.io/s/chapter-49", page(top=vortex, story_seed=49), page(story_seed=490))
        chapter("https://comizy.io/s/chapter-48", page(story_seed=48), page(story_seed=480))
        chapter("https://comizy.io/s/chapter-47", page(top=vortex, story_seed=47), page(story_seed=470, bottom=lagoon))
        chapter("https://comizy.io/s/chapter-46", page(story_seed=46), page(story_seed=460, bottom=lagoon))
        chapter("https://comizy.io/other/chapter-3", page(top=vortex, story_seed=3), page(story_seed=30))
        chapter("https://comizy.io/clean/chapter-9", page(top=art(8), story_seed=9), page(story_seed=90))

    async def scrape(self, url):
        n = len(self.pages.get(url, []))
        return {"images": [f"{url}#{i}" for i in range(n)]}

    async def fetch(self, client, image_url, chapter_url):
        url, i = image_url.split("#")
        return jpeg(self.pages[url][int(i)])

    async def trim(self, url):
        with patch("server.storyteller.fetch_image", self.fetch):
            return await banners.trim_for(url, self.scrape)

    def assertCut(self, trim, skip, px, delta=15):
        self.assertIsNotNone(trim)
        self.assertEqual(trim["skip"], skip)
        self.assertAlmostEqual(trim["px"], px, delta=delta)

    async def test_a_banner_repeated_on_a_nearby_chapter_is_trimmed_and_learned(self):
        r = await self.trim("https://comizy.io/s/chapter-49")
        self.assertCut(r["top"], 0, 480)
        self.assertEqual((r["top"]["width"], r["top"]["height"]), (800, 1250))
        self.assertIsNone(r["bottom"])
        # Learned: another series by the same group, with no neighbours to compare.
        r = await self.trim("https://comizy.io/other/chapter-3")
        self.assertCut(r["top"], 0, 480)
        # And it survives a restart.
        banners.reset()
        r = await self.trim("https://comizy.io/other/chapter-3")
        self.assertCut(r["top"], 0, 480)

    async def test_bottom_banners_too(self):
        r = await self.trim("https://comizy.io/s/chapter-47")
        self.assertCut(r["bottom"], 0, 380)
        self.assertEqual((r["bottom"]["width"], r["bottom"]["height"]), (800, 1250))

    async def test_artwork_that_never_repeats_is_left_alone(self):
        r = await self.trim("https://comizy.io/clean/chapter-9")
        self.assertEqual(r, {"top": None, "bottom": None})

    async def test_a_known_banner_with_no_gap_after_it(self):
        await self.trim("https://comizy.io/s/chapter-49")   # learns the banner
        joined = art(66, 800, 1250)
        joined.paste(art(1), (0, 0))                         # the art runs straight on from it
        self.pages["https://comizy.io/s/chapter-45"] = [joined, page(story_seed=451), page(story_seed=450)]
        r = await self.trim("https://comizy.io/s/chapter-45")
        self.assertCut(r["top"], 0, 480)

    async def test_a_notice_across_two_images_at_both_ends_goes(self):
        notice = art(21, 800, 1700)
        first, second = notice.crop((0, 0, 800, 1250)), page(story_seed=5)
        second.paste(notice.crop((0, 1250, 800, 1700)), (0, 0))
        before_last, last = page(story_seed=6), notice.crop((0, 450, 800, 1700))
        before_last.paste(notice.crop((0, 0, 800, 450)), (0, 800))
        url = "https://comizy.io/d/chapter-48"
        self.pages[url] = [first, second, page(story_seed=7), before_last, last]
        r = await self.trim(url)
        self.assertCut(r["top"], 1, 450)
        self.assertCut(r["bottom"], 1, 450)
        # Learned too: the same notice opening another series' chapter.
        other = "https://comizy.io/e/chapter-2"
        self.pages[other] = [first, second, page(story_seed=8), page(story_seed=9)]
        r = await self.trim(other)
        self.assertCut(r["top"], 1, 450)

    async def test_a_credits_page_seen_on_another_series_of_the_same_site_goes_whole(self):
        credits = art(31, 1200, 1076)
        self.pages["https://comizy.io/a/chapter-48"] = [credits, page(story_seed=1), page(story_seed=11)]
        self.pages["https://comizy.io/b/chapter-12"] = [credits, page(story_seed=2), page(story_seed=12)]
        self.assertIsNone((await self.trim("https://comizy.io/a/chapter-48"))["top"])
        r = await self.trim("https://comizy.io/b/chapter-12")
        self.assertEqual(r["top"], {"skip": 1, "px": 0, "width": 800, "height": 1250})

    async def test_the_same_chapter_on_another_site_is_not_a_repeat(self):
        first = page(top=art(30), story_seed=31)
        self.pages["https://comizy.io/c/chapter-5"] = [first, page(story_seed=32), page(story_seed=33)]
        self.pages["https://mangaread.org/manga/c/chapter-5/"] = [first, page(story_seed=32), page(story_seed=33)]
        await self.trim("https://comizy.io/c/chapter-5")
        r = await self.trim("https://mangaread.org/manga/c/chapter-5/")
        self.assertIsNone(r["top"])

    async def test_a_chapter_with_nothing_found_is_looked_at_again_later(self):
        url = "https://comizy.io/other/chapter-3"
        self.assertIsNone((await self.trim(url))["top"])     # nothing known yet, no neighbours
        await self.trim("https://comizy.io/s/chapter-49")    # now the banner is learned
        self.assertIsNone((await self.trim(url))["top"])     # a recent answer still stands
        when, result = banners._results[url]
        banners._results[url] = (when - banners.RECHECK_AFTER - 1, result)
        self.assertCut((await self.trim(url))["top"], 0, 480)
