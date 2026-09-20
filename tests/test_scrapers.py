import unittest

from scrapers import clean_image_urls, extract_image_urls_from_html


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


if __name__ == "__main__":
    unittest.main()
