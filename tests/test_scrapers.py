import unittest

from scrapers import clean_image_urls, extract_chapter_list, extract_image_urls_from_html


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


if __name__ == "__main__":
    unittest.main()
