import unittest
from unittest.mock import AsyncMock, patch

from scrapers import clean_image_urls, extract_chapter_list, extract_image_urls_from_html
from scrapers.core import (
    _best_match,
    _detect_domain,
    scrape_comizy,
    scrape_comizy_chapter_list,
    scrape_flamecomics,
    scrape_flamecomics_chapter_list,
    scrape_kaliscan_chapter_list,
    scrape_mangadex,
    scrape_mangadex_chapter_list,
    scrape_mangakatana_chapter_list,
    scrape_weebcentral,
    search_comizy,
    search_flamecomics,
    search_mangadex,
)


class ScraperParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_image_urls_filters_ads_and_duplicates(self):
        urls = [
            "https://example.com/page-1.jpg",
            "//example.com/logo.png",
            "https://example.com/banner.jpg",
            "https://example.com/discord.jpg",
            "https://example.com/page-1.jpg",
            "https://example.com/page-2.webp",
        ]
        self.assertEqual(
            clean_image_urls(urls),
            ["https://example.com/page-1.jpg", "https://example.com/page-2.webp"],
        )

    def test_clean_image_urls_does_not_false_positive_on_ad_substring(self):
        # "ad" as a bare substring match used to nuke every image for any
        # title containing it as a fragment of another word — e.g. "shadow".
        urls = [
            "https://cdn.example.com/shadow-slave/9/b989e1.webp?v=123",
            "https://cdn.example.com/gradient-cover.jpg",
            "https://cdn.example.com/loaded-page.png",
        ]
        self.assertEqual(clean_image_urls(urls), urls)

    def test_clean_image_urls_still_filters_real_ad_paths(self):
        urls = [
            "https://cdn.example.com/page-1.jpg",
            "https://cdn.example.com/ads/banner-ad.jpg",
            "https://cdn.example.com/ad-placement.png",
        ]
        self.assertEqual(clean_image_urls(urls), ["https://cdn.example.com/page-1.jpg"])

    def test_toongod_html_extraction_prefers_data_attributes(self):
        html = """
        <div class="reading-content">
            <img data-src="https://cdn.example.com/1.jpg">
            <img data-lazy-src="https://cdn.example.com/2.webp">
            <img src="https://cdn.example.com/logo.png">
        </div>
        <div class="page-break"><img src="https://cdn.example.com/3.jpg"></div>
        <img class="wp-manga-chapter-img" src="https://cdn.example.com/4.png">
        """
        result = extract_image_urls_from_html(
            html,
            [
                ".reading-content img",
                ".page-break img",
                "img.wp-manga-chapter-img",
            ],
        )
        self.assertIn("https://cdn.example.com/1.jpg", result)
        self.assertIn("https://cdn.example.com/2.webp", result)
        self.assertIn("https://cdn.example.com/3.jpg", result)
        self.assertIn("https://cdn.example.com/4.png", result)

    def test_asura_html_extraction_from_readerarea(self):
        html = """
        <div id="readerarea">
            <img src="https://asura.example.com/ch1.jpg">
            <img src="https://asura.example.com/discord.jpg">
        </div>
        """
        result = extract_image_urls_from_html(
            html,
            ["#readerarea img", "#chapter-images img"],
        )
        self.assertIn("https://asura.example.com/ch1.jpg", result)

    def test_extract_chapter_list_from_asura_style_markup(self):
        html = """
        <div>
            <a href="/comics/shadow-slave/chapter/9">Chapter 9</a>
            <a href="/comics/shadow-slave/chapter/1">First Chapter</a>
            <a href="/comics/shadow-slave/chapter/2">Chapter 2</a>
        </div>
        """
        chapters = extract_chapter_list(
            html, 'a[href*="/chapter/"]', "https://asurascans.com/"
        )
        self.assertEqual([c["number"] for c in chapters], [1.0, 2.0, 9.0])
        self.assertEqual(
            chapters[0]["url"], "https://asurascans.com/comics/shadow-slave/chapter/1"
        )

    def test_extract_chapter_list_from_madara_style_markup(self):
        html = """
        <li class="wp-manga-chapter"><a href="/manga/foo/chapter-12/">Chapter 12</a></li>
        <li class="wp-manga-chapter"><a href="/manga/foo/chapter-1/">Chapter 1</a></li>
        """
        chapters = extract_chapter_list(html, ".wp-manga-chapter a", "https://toongod.org/")
        self.assertEqual([c["number"] for c in chapters], [1.0, 12.0])

    def test_extract_chapter_list_deduplicates_by_number(self):
        html = """
        <a href="/comics/foo/chapter/1">Chapter 1</a>
        <a href="/comics/foo/chapter/1">Chapter 1 (again in another widget)</a>
        """
        chapters = extract_chapter_list(
            html, 'a[href*="/chapter/"]', "https://asurascans.com/"
        )
        self.assertEqual(len(chapters), 1)

    def test_extract_chapter_list_from_mangafreak_style_markup(self):
        # MangaFreak's chapter URLs don't contain the word "chapter" at
        # all, and the link text has a subtitle after the number
        # ("Chapter 1 - Romance Dawn") so it isn't at the end either —
        # only the trailing-number-in-href fallback can read these.
        html = """
        <a class="chapter-link" href="/Read1_One_Piece_2">Chapter 2 - They Call Him Strawhat Luffy</a>
        <a class="chapter-link" href="/Read1_One_Piece_1">Chapter 1 - Romance Dawn</a>
        <a class="chapter-link" href="/Read1_One_Piece_10">Chapter 10 - Ordeal</a>
        """
        chapters = extract_chapter_list(html, "a.chapter-link", "https://ww3.mangafreak.me/")
        self.assertEqual([c["number"] for c in chapters], [1.0, 2.0, 10.0])
        self.assertEqual(
            chapters[0]["url"], "https://ww3.mangafreak.me/Read1_One_Piece_1"
        )


class SourceMatchingTests(unittest.TestCase):
    def test_best_match_picks_the_right_card_by_cover_alt_text(self):
        html = """
        <a href="/comics/shadow-slave-05c7df14"><img alt="Shadow Slave"></a>
        <a href="/comics/some-other-manga-abc"><img alt="Some Other Manga"></a>
        """
        url = _best_match(
            html, 'a[href^="/comics/"]', "https://asurascans.com/comics",
            "Shadow Slave", use_alt=True,
        )
        self.assertEqual(url, "https://asurascans.com/comics/shadow-slave-05c7df14")

    def test_best_match_ignores_extra_text_around_the_title(self):
        # Real cards mix a rating into the link's own text (e.g. "9.4 Shadow
        # Slave") — the cover image's alt text is what should be matched.
        html = '<a href="/comics/shadow-slave-05c7df14">9.4 <img alt="Shadow Slave"></a>'
        url = _best_match(
            html, 'a[href^="/comics/"]', "https://asurascans.com/comics",
            "Shadow Slave", use_alt=True,
        )
        self.assertEqual(url, "https://asurascans.com/comics/shadow-slave-05c7df14")

    def test_best_match_finds_a_titled_anchor_after_a_nameless_one_sharing_its_href(self):
        # MangaFreak's search cards wrap two anchors around the same href —
        # a bare cover-image link first, a titled link second. The bare
        # one used to get marked "seen" before its emptiness was checked,
        # which blocked the titled anchor right after it from ever being
        # scored.
        html = """
        <div class="manga_search_item">
            <a href="/Manga/One_Piece"><img src="https://x/one_piece.jpg"></a>
            <a href="/Manga/One_Piece">One Piece</a>
        </div>
        """
        url = _best_match(
            html, '.manga_search_item a[href^="/Manga/"]',
            "https://ww3.mangafreak.me/Find/one%20piece", "One Piece", use_alt=False,
        )
        self.assertEqual(url, "https://ww3.mangafreak.me/Manga/One_Piece")

    def test_best_match_returns_none_below_threshold(self):
        html = '<a href="/comics/totally-unrelated"><img alt="Totally Unrelated Thing"></a>'
        url = _best_match(
            html, 'a[href^="/comics/"]', "https://asurascans.com/comics",
            "Shadow Slave", use_alt=True,
        )
        self.assertIsNone(url)


def _mock_json_response(payload):
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload
    return response


class MangaDexTests(unittest.IsolatedAsyncioTestCase):
    def test_detect_domain_recognizes_mangadex(self):
        self.assertEqual(_detect_domain("https://mangadex.org/title/abc-123"), "mangadex")

    async def test_search_picks_the_manga_whose_alt_title_matches_best(self):
        payload = {
            "data": [
                {
                    "id": "wrong-series",
                    "attributes": {"title": {"en": "Totally Unrelated Manga"}, "altTitles": []},
                },
                {
                    "id": "right-series",
                    "attributes": {
                        "title": {"ko-ro": "Na Honjaman Level-Up"},
                        "altTitles": [{"ko": "나 혼자만 레벨업"}, {"en": "Solo Leveling"}],
                    },
                },
            ]
        }
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            url = await search_mangadex("Solo Leveling")
        self.assertEqual(url, "https://mangadex.org/title/right-series")

    async def test_search_returns_none_below_threshold(self):
        payload = {"data": [{"id": "x", "attributes": {"title": {"en": "Nothing Alike"}, "altTitles": []}}]}
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            url = await search_mangadex("Shadow Slave")
        self.assertIsNone(url)

    async def test_search_returns_none_on_network_failure(self):
        with patch("httpx.AsyncClient.get", side_effect=RuntimeError("network down")):
            url = await search_mangadex("Solo Leveling")
        self.assertIsNone(url)

    async def test_chapter_list_skips_licensed_out_and_pageless_entries(self):
        payload = {
            "data": [
                {
                    "id": "ch-external",
                    "attributes": {"chapter": "5", "pages": 0, "externalUrl": "https://example.com/read/5"},
                },
                {
                    "id": "ch-empty",
                    "attributes": {"chapter": "4", "pages": 0, "externalUrl": None},
                },
                {
                    "id": "ch-3",
                    "attributes": {"chapter": "3", "pages": 20, "externalUrl": None, "title": "Chapter 3"},
                },
                {
                    "id": "ch-1",
                    "attributes": {"chapter": "1", "pages": 18, "externalUrl": None, "title": None},
                },
            ]
        }
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            chapters = await scrape_mangadex_chapter_list("https://mangadex.org/title/abcd1234-ef56-7890-abcd-ef1234567890")
        self.assertEqual(
            chapters,
            [
                {"number": 1.0, "url": "https://mangadex.org/chapter/ch-1", "title": "Chapter 1"},
                {"number": 3.0, "url": "https://mangadex.org/chapter/ch-3", "title": "Chapter 3"},
            ],
        )

    async def test_chapter_list_returns_empty_without_a_valid_id_in_the_url(self):
        chapters = await scrape_mangadex_chapter_list("https://mangadex.org/title/not-a-uuid")
        self.assertEqual(chapters, [])

    async def test_scrape_builds_image_urls_from_base_url_hash_and_filenames(self):
        payload = {
            "baseUrl": "https://cdn.example.mangadex.network",
            "chapter": {"hash": "deadbeef", "data": ["1-aaa.jpg", "2-bbb.png"]},
        }
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            images = await scrape_mangadex("https://mangadex.org/chapter/abcd1234-ef56-7890-abcd-ef1234567890")
        self.assertEqual(
            images,
            [
                "https://cdn.example.mangadex.network/data/deadbeef/1-aaa.jpg",
                "https://cdn.example.mangadex.network/data/deadbeef/2-bbb.png",
            ],
        )

    async def test_scrape_returns_empty_when_response_is_missing_hash(self):
        payload = {"baseUrl": "https://cdn.example.mangadex.network", "chapter": {}}
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            images = await scrape_mangadex("https://mangadex.org/chapter/abcd1234-ef56-7890-abcd-ef1234567890")
        self.assertEqual(images, [])


class ComizyTests(unittest.IsolatedAsyncioTestCase):
    def test_detect_domain_recognizes_comizy_and_its_old_domain(self):
        self.assertEqual(_detect_domain("https://comizy.io/solo-leveling"), "comizy")
        self.assertEqual(_detect_domain("https://mangabuddy.com/solo-leveling"), "comizy")

    async def test_search_skips_adult_and_dmca_titles(self):
        payload = {
            "data": {
                "items": [
                    {"url": "/solo-leveling-adult-edit", "name": "Solo Leveling", "alt_names": [], "is_adult": True},
                    {"url": "/solo-leveling-dmca", "name": "Solo Leveling", "alt_names": [], "has_dmca": True},
                    {"url": "/solo-leveling", "name": "Solo Leveling", "alt_names": [{"name": "Na Honjaman Level-Up"}]},
                ]
            }
        }
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            url = await search_comizy("Solo Leveling")
        self.assertEqual(url, "https://comizy.io/solo-leveling")

    async def test_search_returns_none_below_threshold(self):
        payload = {"data": {"items": [{"url": "/x", "name": "Nothing Alike", "alt_names": []}]}}
        with patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            url = await search_comizy("Shadow Slave")
        self.assertIsNone(url)

    async def test_search_returns_none_on_network_failure(self):
        with patch("httpx.AsyncClient.get", side_effect=RuntimeError("network down")):
            url = await search_comizy("Solo Leveling")
        self.assertIsNone(url)

    async def test_chapter_list_reads_title_id_from_the_page_and_dedupes_by_number(self):
        page_html = '<script>{"id":"4N90moOv","is_adult":false}</script>'
        payload = {
            "data": {
                "chapters": [
                    {"id": "b", "url": "/solo-leveling/chapter-2", "name": "Chapter 2", "number": 2},
                    {"id": "a", "url": "/solo-leveling/chapter-1", "name": "Chapter 1", "number": 1},
                    {"id": "a-dup", "url": "/solo-leveling/chapter-1-again", "name": "Chapter 1 (dup)", "number": 1},
                ]
            }
        }
        with patch("scrapers.core.fetch_html_httpx", return_value=page_html), \
             patch("httpx.AsyncClient.get", return_value=_mock_json_response(payload)):
            chapters = await scrape_comizy_chapter_list("https://comizy.io/solo-leveling")
        self.assertEqual(
            chapters,
            [
                {"number": 1.0, "url": "https://comizy.io/solo-leveling/chapter-1", "title": "Chapter 1"},
                {"number": 2.0, "url": "https://comizy.io/solo-leveling/chapter-2", "title": "Chapter 2"},
            ],
        )

    async def test_chapter_list_blocks_adult_titles_reached_by_direct_url(self):
        page_html = '<script>{"id":"4N90moOv","is_adult":true}</script>'
        with patch("scrapers.core.fetch_html_httpx", return_value=page_html), \
             patch("httpx.AsyncClient.get", return_value=_mock_json_response({"data": {"chapters": []}})) as mock_get:
            chapters = await scrape_comizy_chapter_list("https://comizy.io/some-adult-title")
        self.assertEqual(chapters, [])
        mock_get.assert_not_called()

    async def test_scrape_extracts_page_images_in_reader_order(self):
        page_html = (
            '<script>{"id":"4N90moOv","is_adult":false}</script>'
            '<div data-page-idx="0"><img src="https://x7.cmzcdn.org/e/aaa.webp"></div>'
            '<div data-page-idx="1"><img src="https://x8.cmzcdn.org/e/bbb.webp"></div>'
        )
        with patch("scrapers.core.fetch_html_httpx", return_value=page_html):
            images = await scrape_comizy("https://comizy.io/solo-leveling/chapter-1")
        self.assertEqual(
            images,
            ["https://x7.cmzcdn.org/e/aaa.webp", "https://x8.cmzcdn.org/e/bbb.webp"],
        )

    async def test_scrape_blocks_adult_chapters_reached_by_direct_url(self):
        page_html = (
            '<script>{"id":"4N90moOv","is_adult":true}</script>'
            '<div data-page-idx="0"><img src="https://x7.cmzcdn.org/e/aaa.webp"></div>'
        )
        with patch("scrapers.core.fetch_html_httpx", return_value=page_html):
            images = await scrape_comizy("https://comizy.io/some-adult-title/chapter-1")
        self.assertEqual(images, [])


class NewDomainDetectionTests(unittest.TestCase):
    def test_detect_domain_recognizes_each_new_source(self):
        cases = {
            "https://www.mangaread.org/manga/solo-leveling-manhwa/": "mangaread",
            "https://flamecomics.xyz/series/1": "flamecomics",
            "https://manhuaplus.org/manga/solo-leveling-ragnarok": "manhuaplus",
            "https://kaliscan.io/manga/33304-leveling-up-alone": "kaliscan",
            "https://mangakatana.com/manga/solo-leveling.21708": "mangakatana",
            "https://weebcentral.com/series/01J76XYCPSY3C4BNPBRY8JMCBE/Solo-Leveling": "weebcentral",
        }
        for url, expected in cases.items():
            self.assertEqual(_detect_domain(url), expected)


FLAMECOMICS_NEXT_DATA_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">{}</script>'
)


def _flamecomics_html(page_props):
    import json

    return FLAMECOMICS_NEXT_DATA_HTML.format(
        json.dumps({"props": {"pageProps": page_props}})
    )


class FlameComicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_scores_the_whole_catalog_since_there_is_no_search_endpoint(self):
        html = _flamecomics_html({
            "series": [
                {"series_id": 99, "title": "Totally Unrelated Manga"},
                {"series_id": 1, "title": "Solo Leveling"},
            ]
        })
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            url = await search_flamecomics("Solo Leveling")
        self.assertEqual(url, "https://flamecomics.xyz/series/1")

    async def test_search_returns_none_below_threshold(self):
        html = _flamecomics_html({"series": [{"series_id": 1, "title": "Nothing Alike"}]})
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            url = await search_flamecomics("Shadow Slave")
        self.assertIsNone(url)

    async def test_search_returns_none_on_network_failure(self):
        with patch("scrapers.core.fetch_html_httpx", side_effect=RuntimeError("network down")):
            url = await search_flamecomics("Solo Leveling")
        self.assertIsNone(url)

    async def test_chapter_list_reads_from_next_data_json_and_dedupes(self):
        html = _flamecomics_html({
            "chapters": [
                {"chapter": "2.00", "token": "tok2", "title": "Chapter 2"},
                {"chapter": "1.00", "token": "tok1", "title": "Chapter 1"},
                {"chapter": "1.00", "token": "tok1-dup", "title": "Chapter 1 (dup)"},
                {"chapter": "3.00", "token": None, "title": "Missing token, skipped"},
            ]
        })
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            chapters = await scrape_flamecomics_chapter_list("https://flamecomics.xyz/series/1")
        self.assertEqual(
            chapters,
            [
                {"number": 1.0, "url": "https://flamecomics.xyz/series/1/tok1", "title": "Chapter 1"},
                {"number": 2.0, "url": "https://flamecomics.xyz/series/1/tok2", "title": "Chapter 2"},
            ],
        )

    async def test_scrape_excludes_decoy_promo_images_by_path(self):
        html = (
            '<img src="https://cdn.flamecomics.xyz/uploads/images/series/1/tok/SL-1-0.jpg?123">'
            '<img src="https://cdn.flamecomics.xyz/assets/read/read_on_flame_1.webp" style="display:none">'
        )
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            images = await scrape_flamecomics("https://flamecomics.xyz/series/1/tok")
        self.assertEqual(images, ["https://cdn.flamecomics.xyz/uploads/images/series/1/tok/SL-1-0.jpg?123"])


class KaliscanChapterListTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_title_excludes_the_timestamp_sharing_its_anchor(self):
        html = """
        <ul id="chapter-list">
          <li><a href="/manga/x/chapter-2" title="X - Chapter 2">
            <div><strong class="chapter-title">Chapter 2</strong><time class="chapter-update">2 years ago</time></div>
          </a></li>
          <li><a href="/manga/x/chapter-1" title="X - Chapter 1">
            <div><strong class="chapter-title">Chapter 1</strong><time class="chapter-update">3 years ago</time></div>
          </a></li>
        </ul>
        """
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            chapters = await scrape_kaliscan_chapter_list("https://kaliscan.io/manga/x")
        self.assertEqual(
            chapters,
            [
                {"number": 1.0, "url": "https://kaliscan.io/manga/x/chapter-1", "title": "Chapter 1"},
                {"number": 2.0, "url": "https://kaliscan.io/manga/x/chapter-2", "title": "Chapter 2"},
            ],
        )


class MangaKatanaChapterListTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_number_is_read_from_the_bare_c_number_url_suffix(self):
        html = """
        <div class="chapters"><table class="uk-table">
          <tr><div class="chapter"><a href="/manga/solo-leveling.21708/c2">Chapter 2</a></div></tr>
          <tr><div class="chapter"><a href="/manga/solo-leveling.21708/c1">Chapter 1</a></div></tr>
          <tr><div class="chapter"><a href="/manga/solo-leveling.21708/c0">Chapter 0</a></div></tr>
        </table></div>
        """
        with patch("scrapers.core.fetch_html_httpx", return_value=html):
            chapters = await scrape_mangakatana_chapter_list("https://mangakatana.com/manga/solo-leveling.21708")
        self.assertEqual(
            [c["number"] for c in chapters], [0.0, 1.0, 2.0]
        )
        self.assertEqual(
            chapters[1]["url"], "https://mangakatana.com/manga/solo-leveling.21708/c1"
        )


class WeebCentralTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_fetches_the_images_fragment_endpoint_and_parses_it(self):
        fragment_html = (
            '<section id="chapter-images">'
            '<img src="https://hot.planeptune.us/manga/Solo-Leveling/0001-001.png">'
            '<img src="https://hot.planeptune.us/manga/Solo-Leveling/0001-002.png">'
            "</section>"
        )
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.text = fragment_html
        with patch("httpx.AsyncClient.get", return_value=mock_response):
            images = await scrape_weebcentral("https://weebcentral.com/chapters/01J76XYXYZHAP5EMZ62S0G3WGA")
        self.assertEqual(
            images,
            [
                "https://hot.planeptune.us/manga/Solo-Leveling/0001-001.png",
                "https://hot.planeptune.us/manga/Solo-Leveling/0001-002.png",
            ],
        )

    async def test_scrape_returns_empty_without_a_chapter_id_in_the_url(self):
        images = await scrape_weebcentral("https://weebcentral.com/not-a-chapter-url")
        self.assertEqual(images, [])


if __name__ == "__main__":
    unittest.main()
