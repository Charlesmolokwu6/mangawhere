import asyncio
import tempfile
import unittest
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from scrapers import reddit
from server import cache, db


def feed(title, op, comments, subreddit="manhwa"):
    def entry(author, entry_title, html):
        return (f"<entry><author><name>/u/{author}</name></author>"
                f'<category term="{subreddit}" label="r/{subreddit}"/>'
                f"<content type=\"html\">{escape(html)}</content><title>{escape(entry_title)}</title></entry>")
    body = entry(op, title, '<!-- SC_OFF --><div class="md"><p>Guy gets put into a fantasy world.</p></div>'
                            '<!-- SC_ON --> &#32; submitted by &#32; <a href="x">/u/op</a>')
    body += "".join(entry(a, f"/u/{a} on {title}", f'<div class="md"><p>{h}</p></div>') for a, h in comments)
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


SAUCE = feed("[Help Finding Name of Manhwa]", "asker", [
    ("AutoModerator", "Report the post if you think this is LOW-EFFORT. **Status** check."),
    ("helper1", "Primal hunter?"),
    ("asker", "Yes, this is it thank you"),
    ("helper2", "I'm pretty sure the top one is There's a Demon Lord on the Floor"),
    ("chatty", "it's getting an anime"),
    ("chatty2", 'I read it as the "bitter" end of things, called MAGA in the story.'),
    ("helper3", "Is it good?"),
    ("helper4", "<strong>Solo Leveling</strong> for sure"),
])


class UrlTests(unittest.TestCase):
    def test_post_ids_from_every_kind_of_post_link(self):
        for url in ("https://www.reddit.com/r/manhwa/comments/1wth1vm/help_finding_name/",
                    "https://old.reddit.com/r/manga/comments/1WTH1VM/",
                    "https://www.reddit.com/comments/1wth1vm",
                    "https://redd.it/1wth1vm",
                    "https://www.reddit.com/r/manhwa/comments/1wth1vm/x/abc123/"):
            self.assertEqual(reddit.post_id_from_url(url), "1wth1vm", url)

    def test_share_links_and_other_sites(self):
        share = "https://www.reddit.com/r/manhwa/s/AbC123xyz"
        self.assertIsNone(reddit.post_id_from_url(share))
        self.assertTrue(reddit.is_share_link(share))
        self.assertIsNone(reddit.post_id_from_url("https://evil.example/r/x/comments/1wth1vm/"))
        self.assertFalse(reddit.is_reddit("https://reddit.com.evil.example/r/x"))


class ParseTests(unittest.TestCase):
    def test_the_post_and_its_comments_come_out_of_the_feed(self):
        post = reddit.parse_feed(SAUCE)
        self.assertEqual(post["title"], "[Help Finding Name of Manhwa]")
        self.assertEqual(post["text"], "Guy gets put into a fantasy world.")
        self.assertEqual(post["author"], "asker")
        self.assertEqual(post["subreddit"], "r/manhwa")
        self.assertEqual(len(post["comments"]), 8)
        self.assertTrue(reddit.looks_like_question(post))

    def test_candidates_skip_bots_the_asker_questions_and_remarks(self):
        names = [c["name"] for c in reddit.candidates(reddit.parse_feed(SAUCE))]
        self.assertIn("Primal hunter", names)
        self.assertIn("There's a Demon Lord on the Floor", names)
        self.assertIn("Solo Leveling", names)
        for noise in ("Status", "Yes, this is it thank you", "bitter", "MAGA", "Is it good", "good"):
            self.assertNotIn(noise, names)

    def test_matching_needs_the_real_name_not_just_a_search_hit(self):
        hunter = {"title": {"english": "The Primal Hunter"}, "synonyms": []}
        self.assertTrue(reddit.matches("Primal hunter", hunter))
        self.assertTrue(reddit.matches("Atsumare fushigi kenkyubu",
                                       {"title": {"romaji": "Atsumare! Fushigi Kenkyuubu"}}))
        self.assertFalse(reddit.matches("give it time", {"title": {"english": "Give It Up"}}))
        # One word: only the exact main title, and only when the reply stressed it.
        self.assertTrue(reddit.matches("Gokurakugai", {"title": {"romaji": "Gokurakugai"}}, strong=True))
        self.assertFalse(reddit.matches("Gokurakugai", {"title": {"romaji": "Gokurakugai"}}, strong=False))
        self.assertFalse(reddit.matches("Know", {"title": {"romaji": "X"}, "synonyms": ["Know"]}))

    def test_verify_keeps_confirmed_titles_and_offers_unknown_answers_as_maybe(self):
        found = reddit.candidates(reddit.parse_feed(SAUCE))
        data = {}
        for i, c in enumerate(found):
            if c["name"] == "Solo Leveling":
                data[f"c{i}"] = {"media": [{"id": 105398, "title": {"english": "Solo Leveling"}, "synonyms": []}]}
            elif c["name"].startswith("There's a Demon"):
                data[f"c{i}"] = {"media": [{"id": 1, "title": {"english": "There's a Demon Lord on the Floor"}}]}
            else:
                data[f"c{i}"] = {"media": [{"id": 9, "title": {"english": "Something Else Entirely"}}]}
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": data}
        client = MagicMock(post=AsyncMock(return_value=response))
        out = asyncio.run(reddit.verify(client, found))
        self.assertEqual([t["title"] for t in out["titles"]],
                         ["Solo Leveling", "There's a Demon Lord on the Floor"])
        self.assertEqual(out["maybe"], ["Primal hunter"])


def listing():
    def comment(author, html=None, body=None):
        data = {"author": author}
        if html is not None:
            data["body_html"] = escape(f'<div class="md"><p>{html}</p></div>')
        if body is not None:
            data["body"] = body
        return {"kind": "t1", "data": data}
    return [
        {"data": {"children": [{"kind": "t3", "data": {
            "id": "1WTH1VM", "title": "What manga is this from?", "author": "asker",
            "subreddit_name_prefixed": "r/manga",
            "selftext_html": escape('<div class="md"><p>Saw this <b>panel</b>.</p></div>')}}]}},
        {"data": {"children": [
            comment("kein23", html="Gokurakugai"),
            comment("markdown_only", body="**Solo Leveling** obviously"),
            comment("asker", html="Thanks so much!"),
            {"kind": "more", "data": {}},
        ]}},
    ]


class ListingTests(unittest.TestCase):
    def test_a_post_fetched_by_the_phone_reads_like_the_feed(self):
        post = reddit.parse_listing(listing())
        self.assertEqual(post["id"], "1wth1vm")
        self.assertEqual(post["title"], "What manga is this from?")
        self.assertEqual(post["text"], "Saw this panel.")
        self.assertEqual(post["subreddit"], "r/manga")
        self.assertEqual([c["author"] for c in post["comments"]], ["kein23", "markdown_only", "asker"])
        names = [c["name"] for c in reddit.candidates(post)]
        self.assertEqual(names[0], "Gokurakugai")
        self.assertIn("Solo Leveling", names)
        self.assertNotIn("Thanks so much", names)

    def test_anything_else_is_not_a_post(self):
        for junk in (None, {}, [], [{"data": {}}], "text", [{"data": {"children": []}}]):
            self.assertEqual(reddit.parse_listing(junk), {}, junk)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        cache.clear_memory()
        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_answers(self):
        result = {"id": "1wth1vm", "title": "t", "text": "", "titles": [], "maybe": ["Primal hunter"]}
        with patch.object(reddit, "lookup", AsyncMock(return_value=result)):
            r = self.client.get("/api/reddit-lookup", params={"url": "https://redd.it/1wth1vm"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["maybe"], ["Primal hunter"])

    def test_a_post_sent_by_the_phone_is_read(self):
        verified = {"titles": [{"title": "Gokurakugai", "id": 1}], "maybe": []}
        with patch.object(reddit, "verify", AsyncMock(return_value=verified)):
            r = self.client.post("/api/reddit-read", json=listing())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["titles"][0]["title"], "Gokurakugai")
        self.assertTrue(r.json()["question"])
        self.assertEqual(self.client.post("/api/reddit-read", content=b"not json").status_code, 400)
        self.assertEqual(self.client.post("/api/reddit-read", json={"x": 1}).status_code, 400)
        big = b"[" + b" " * 3_000_001 + b"]"
        self.assertEqual(self.client.post("/api/reddit-read", content=big).status_code, 413)

    def test_errors(self):
        url = {"url": "https://redd.it/1wth1vm"}
        self.assertEqual(self.client.get("/api/reddit-lookup",
                                         params={"url": "https://example.com/x"}).status_code, 400)
        with patch.object(reddit, "lookup", AsyncMock(side_effect=reddit.RedditBusy("429"))):
            self.assertEqual(self.client.get("/api/reddit-lookup", params=url).status_code, 503)
        with patch.object(reddit, "lookup", AsyncMock(return_value={})):
            self.assertEqual(self.client.get("/api/reddit-lookup", params=url).status_code, 404)


if __name__ == "__main__":
    unittest.main()
