#!/usr/bin/env python3
"""Regression tests for the profile README generator.

Run with:

    python3 -m unittest discover -s .github/scripts -p "test_*.py" -v

The generator rewrites a file that also holds hand-written content, so the
contract that matters most is: only the text between the markers may change.
These tests pin that contract together with the marker, feed and rendering
behaviour.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("update_readme.py")

_spec = importlib.util.spec_from_file_location("update_readme", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
update_readme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(update_readme)


TEMPLATE = """# Profile

Hand written intro that must never change.

## Stats

<!-- AUTO-STATS-START -->
<!-- AUTO-STATS-END -->

## Repos

<!-- AUTO-REPOS-START -->
<!-- AUTO-REPOS-END -->

## Blog

<!-- BLOG-LIST-START -->
<!-- BLOG-LIST-END -->

## Contact

- Keep me exactly as I am.
"""


def make_repo(name, **overrides):
    repo = {
        "name": name,
        "html_url": f"https://github.com/Shino08/{name}",
        "description": f"Description of {name}",
        "language": "TypeScript",
        "stargazers_count": 3,
        "pushed_at": "2026-10-01T12:00:00Z",
        "fork": False,
        "archived": False,
    }
    repo.update(overrides)
    return repo


class MarkerTests(unittest.TestCase):
    def test_replaces_only_the_block_between_markers(self):
        result = update_readme.replace_section(
            TEMPLATE,
            update_readme.AUTO_STATS_START,
            update_readme.AUTO_STATS_END,
            "NEW CONTENT",
        )
        self.assertIn(f"{update_readme.AUTO_STATS_START}\nNEW CONTENT\n{update_readme.AUTO_STATS_END}", result)
        self.assertIn("Hand written intro that must never change.", result)
        self.assertIn("- Keep me exactly as I am.", result)

    def test_rejects_missing_marker(self):
        with self.assertRaises(update_readme.UpdateError):
            update_readme.replace_section(
                TEMPLATE,
                "<!-- MISSING-START -->",
                update_readme.AUTO_STATS_END,
                "x",
            )

    def test_rejects_duplicated_marker(self):
        broken = TEMPLATE.replace(
            update_readme.AUTO_STATS_START,
            update_readme.AUTO_STATS_START + "\n" + update_readme.AUTO_STATS_START,
        )
        with self.assertRaises(update_readme.UpdateError):
            update_readme.replace_section(
                broken,
                update_readme.AUTO_STATS_START,
                update_readme.AUTO_STATS_END,
                "x",
            )

    def test_rejects_reversed_markers(self):
        reversed_text = TEMPLATE.replace(
            f"{update_readme.AUTO_STATS_START}\n{update_readme.AUTO_STATS_END}",
            f"{update_readme.AUTO_STATS_END}\n{update_readme.AUTO_STATS_START}",
        )
        with self.assertRaises(update_readme.UpdateError):
            update_readme.replace_section(
                reversed_text,
                update_readme.AUTO_STATS_START,
                update_readme.AUTO_STATS_END,
                "x",
            )


class FeedTests(unittest.TestCase):
    RSS = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Example</title>
        <item>
          <title>Older post</title>
          <link>https://example.com/older</link>
          <pubDate>Mon, 01 Sep 2025 10:00:00 +0000</pubDate>
        </item>
        <item>
          <title>Newer post</title>
          <link>https://example.com/newer</link>
          <pubDate>Tue, 02 Sep 2025 10:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>
    """

    ATOM = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example</title>
      <entry>
        <title>Atom post</title>
        <link rel="self" href="https://example.com/feed"/>
        <link rel="alternate" href="https://example.com/atom-post"/>
        <published>2025-09-03T10:00:00Z</published>
      </entry>
    </feed>
    """

    def test_parses_rss_items_newest_first(self):
        entries = update_readme.parse_feed(self.RSS)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["title"], "Newer post")
        self.assertEqual(entries[0]["link"], "https://example.com/newer")
        self.assertEqual(entries[0]["date"], "2025-09-02")
        self.assertEqual(entries[1]["title"], "Older post")

    def test_same_day_entries_keep_time_order(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Morning post</title>
              <link>https://example.com/morning</link>
              <pubDate>Mon, 01 Sep 2025 08:00:00 +0000</pubDate>
            </item>
            <item>
              <title>Evening post</title>
              <link>https://example.com/evening</link>
              <pubDate>Mon, 01 Sep 2025 20:00:00 +0000</pubDate>
            </item>
          </channel>
        </rss>
        """
        entries = update_readme.parse_feed(feed)
        self.assertEqual(entries[0]["title"], "Evening post")
        self.assertEqual(entries[0]["date"], "2025-09-01")

    def test_parses_atom_entries_and_prefers_alternate_link(self):
        entries = update_readme.parse_feed(self.ATOM)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Atom post")
        self.assertEqual(entries[0]["link"], "https://example.com/atom-post")
        self.assertEqual(entries[0]["date"], "2025-09-03")

    def test_rejects_invalid_xml(self):
        with self.assertRaises(update_readme.UpdateError):
            update_readme.parse_feed("<not xml")

    def test_render_blog_escapes_square_brackets(self):
        rendered = update_readme.render_blog(
            [{"title": "A [weird] title", "link": "https://example.com/x", "date": "2025-09-01"}]
        )
        self.assertIn("A \\[weird\\] title", rendered)


class RenderingTests(unittest.TestCase):
    def test_stats_render_badges_with_public_repos_stars_and_followers(self):
        user = {"public_repos": 76, "followers": 5}
        repos = [make_repo("a", stargazers_count=4), make_repo("b", stargazers_count=2)]
        rendered = update_readme.render_stats(user, repos)
        self.assertIn("Repositorios-76-", rendered)
        self.assertIn("Estrellas-6-", rendered)
        self.assertIn("Seguidores-5-", rendered)
        self.assertIn("style=for-the-badge", rendered)

    def test_repos_render_a_table_with_a_header(self):
        rendered = update_readme.render_repos([make_repo("kept")], "Shino08")
        self.assertTrue(rendered.startswith("| Proyecto | Stack | Estrellas | Actualizado |"))

    def test_repo_description_pipes_do_not_break_the_table(self):
        repo = make_repo("kept", description="a | b")
        rendered = update_readme.render_repos([repo], "Shino08")
        row = [line for line in rendered.splitlines() if "kept" in line][0]
        self.assertIn("a \\| b", row)

    def test_repos_exclude_profile_repo_forks_and_archived(self):
        repos = [
            make_repo("Shino08"),
            make_repo("forked", fork=True),
            make_repo("old", archived=True),
            make_repo("kept"),
        ]
        rendered = update_readme.render_repos(repos, "Shino08")
        self.assertIn("kept", rendered)
        self.assertNotIn("Shino08](https://github.com/Shino08/Shino08)", rendered)
        self.assertNotIn("forked", rendered)
        self.assertNotIn("old", rendered)

    def test_blog_placeholder_when_no_feed_is_configured(self):
        rendered = update_readme.render_blog(None)
        self.assertIn("BLOG_FEED_URL", rendered)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.readme_path = os.path.join(self.temp_dir.name, "README.md")
        self._original_fetch_user = update_readme.fetch_user
        self._original_fetch_repos = update_readme.fetch_repos
        self._original_fetch_feed = update_readme.fetch_feed
        update_readme.fetch_user = lambda login, token="": {"public_repos": 2, "followers": 3}
        update_readme.fetch_repos = lambda login, token="": [
            make_repo("Shino08"),
            make_repo("kept"),
        ]
        update_readme.fetch_feed = lambda url, timeout=30: [
            {"title": "Post", "link": "https://example.com/post", "date": "2025-09-01"}
        ]

    def tearDown(self):
        update_readme.fetch_user = self._original_fetch_user
        update_readme.fetch_repos = self._original_fetch_repos
        update_readme.fetch_feed = self._original_fetch_feed
        self.temp_dir.cleanup()

    def _write(self, text):
        with open(self.readme_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _read(self):
        with open(self.readme_path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def _run(self):
        return update_readme.main(
            ["--login", "Shino08", "--readme", self.readme_path]
        )

    def test_second_run_is_a_no_op(self):
        self._write(TEMPLATE)
        self.assertEqual(self._run(), 0)
        first = self._read()
        self.assertEqual(self._run(), 0)
        self.assertEqual(first, self._read())

    def test_content_outside_markers_survives_the_run(self):
        self._write(TEMPLATE)
        self._run()
        result = self._read()
        self.assertIn("# Profile", result)
        self.assertIn("Hand written intro that must never change.", result)
        self.assertIn("- Keep me exactly as I am.", result)

    def test_aborts_without_writing_when_a_marker_is_missing(self):
        broken = TEMPLATE.replace(update_readme.BLOG_LIST_START, "")
        self._write(broken)
        with self.assertRaises(update_readme.UpdateError):
            self._run()
        self.assertEqual(self._read(), broken)

    def test_crlf_line_endings_are_preserved(self):
        self._write(TEMPLATE.replace("\n", "\r\n"))
        self._run()
        raw = self._read()
        self.assertIn("\r\n", raw)
        # No bare LF may survive: every newline must belong to a CRLF pair.
        self.assertEqual(raw.replace("\r\n", "").count("\n"), 0)

    def test_blog_block_is_filled_when_a_feed_is_configured(self):
        os.environ["BLOG_FEED_URL"] = "https://example.com/feed.xml"
        try:
            self._write(TEMPLATE)
            self._run()
        finally:
            os.environ.pop("BLOG_FEED_URL", None)
        result = self._read()
        self.assertIn("[Post](https://example.com/post)", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
