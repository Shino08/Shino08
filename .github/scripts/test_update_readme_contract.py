#!/usr/bin/env python3
"""Contract regression tests for the profile README generator.

These tests are derived from the feature specification and are independent
of any implementation test that ships with the change. They verify the
guarantees end to end, including the process exit code and the stderr text
produced by a rejected command.

Coverage map (spec invariants):
    V1  content outside the three marker pairs is never touched
    V2  a second run is a no-op and reports "already up to date"
    V3  a missing, duplicated or reversed marker aborts without writing
    V4  the generated blocks do not depend on the input line endings
    V5  the hand-written prose survives and the repo list is filtered
    V6  the workflow keeps its safety properties

Run with:
    python3 -m unittest discover -s .github/scripts -p "test_*.py" -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("update_readme.py")
WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "workflows" / "update-readme.yml"

_spec = importlib.util.spec_from_file_location("update_readme_contract", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)

MARKERS = (
    (generator.AUTO_STATS_START, generator.AUTO_STATS_END),
    (generator.AUTO_REPOS_START, generator.AUTO_REPOS_END),
    (generator.BLOG_LIST_START, generator.BLOG_LIST_END),
)

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


def _repo(name, **overrides):
    payload = {
        "name": name,
        "html_url": f"https://github.com/Shino08/{name}",
        "description": f"Description of {name}",
        "language": "TypeScript",
        "stargazers_count": 3,
        "pushed_at": "2026-01-01T00:00:00Z",
        "fork": False,
        "archived": False,
    }
    payload.update(overrides)
    return payload


# The API fixtures mirror a realistic account: the profile repository itself,
# a fork and an archived repository must never reach the rendered list.
API_USER = {"login": "Shino08", "public_repos": 5, "followers": 3}
API_REPOS = [
    _repo("Shino08", pushed_at="2026-11-01T00:00:00Z", stargazers_count=99),
    _repo("alpha", pushed_at="2026-10-02T00:00:00Z"),
    _repo("forked", pushed_at="2026-10-01T00:00:00Z", fork=True),
    _repo("beta", pushed_at="2026-09-03T00:00:00Z"),
    _repo("archived", pushed_at="2026-09-02T00:00:00Z", archived=True),
]


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def outside_digest(text):
    """Digest of everything outside the three marker interiors."""
    chunks = []
    position = 0
    for start, end in MARKERS:
        begin = text.index(start)
        finish = text.index(end)
        chunks.append(text[position : begin + len(start)])
        position = finish
    chunks.append(text[position:])
    return sha256("".join(chunks))


def block_bodies(text):
    bodies = []
    for start, end in MARKERS:
        bodies.append(text[text.index(start) + len(start) : text.index(end)])
    return bodies


class GeneratorContractTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.readme = os.path.join(self._dir.name, "README.md")
        self._saved = (
            generator.fetch_user,
            generator.fetch_repos,
            generator.fetch_feed,
        )
        generator.fetch_user = lambda login, token="": dict(API_USER)
        generator.fetch_repos = lambda login, token="": [dict(item) for item in API_REPOS]
        generator.fetch_feed = lambda url, timeout=30: []

    def tearDown(self):
        (
            generator.fetch_user,
            generator.fetch_repos,
            generator.fetch_feed,
        ) = self._saved

    def run_generator(self):
        return generator.main(["--login", "Shino08", "--readme", self.readme])

    # ------------------------------------------------------------------ V1
    def test_v1_content_outside_the_markers_is_byte_identical(self):
        write_text(self.readme, TEMPLATE)
        before = read_bytes(self.readme)
        self.assertEqual(self.run_generator(), 0)
        after = read_bytes(self.readme)
        self.assertNotEqual(before, after, "the generator must change the file")
        self.assertEqual(outside_digest(before.decode("utf-8")), outside_digest(after.decode("utf-8")))
        self.assertIn("Hand written intro that must never change.", after.decode("utf-8"))
        self.assertIn("- Keep me exactly as I am.", after.decode("utf-8"))

    # ------------------------------------------------------------------ V2
    def test_v2_second_run_is_a_no_op(self):
        write_text(self.readme, TEMPLATE)
        self.assertEqual(self.run_generator(), 0)
        first = read_bytes(self.readme)
        self.assertEqual(self.run_generator(), 0)
        self.assertEqual(first, read_bytes(self.readme))

    def test_v2_second_run_reports_already_up_to_date(self):
        write_text(self.readme, TEMPLATE)
        self.run_generator()
        printed = io.StringIO()
        stdout, sys.stdout = sys.stdout, printed
        try:
            self.run_generator()
        finally:
            sys.stdout = stdout
        self.assertIn("already up to date", printed.getvalue().lower())

    def test_v2_output_carries_no_generation_timestamp(self):
        write_text(self.readme, TEMPLATE)
        self.run_generator()
        first = read_bytes(self.readme)
        self.run_generator()
        self.assertEqual(first, read_bytes(self.readme))

    # ------------------------------------------------------------------ V3
    def _assert_rejected_without_writing(self, broken, expected):
        write_text(self.readme, broken)
        before = read_bytes(self.readme)
        with self.assertRaises(generator.UpdateError) as caught:
            self.run_generator()
        self.assertIn(expected, str(caught.exception))
        self.assertEqual(before, read_bytes(self.readme), "the file must not change")

    def test_v3_missing_start_marker_aborts_without_writing(self):
        self._assert_rejected_without_writing(
            TEMPLATE.replace(generator.BLOG_LIST_START, ""),
            generator.BLOG_LIST_START,
        )

    def test_v3_missing_end_marker_aborts_without_writing(self):
        self._assert_rejected_without_writing(
            TEMPLATE.replace(generator.AUTO_STATS_END, ""),
            generator.AUTO_STATS_END,
        )

    def test_v3_duplicated_marker_aborts_without_writing(self):
        self._assert_rejected_without_writing(
            TEMPLATE.replace(generator.AUTO_STATS_START, generator.AUTO_STATS_START + "\n" + generator.AUTO_STATS_START),
            generator.AUTO_STATS_START,
        )

    def test_v3_reversed_marker_pair_aborts_without_writing(self):
        self._assert_rejected_without_writing(
            TEMPLATE.replace(
                generator.AUTO_REPOS_START + "\n" + generator.AUTO_REPOS_END,
                generator.AUTO_REPOS_END + "\n" + generator.AUTO_REPOS_START,
            ),
            generator.AUTO_REPOS_START,
        )

    # ------------------------------------------------------------------ V4
    def test_v4_crlf_and_lf_inputs_produce_the_same_blocks(self):
        results = []
        for newline in ("\n", "\r\n"):
            path = os.path.join(self._dir.name, f"README-{len(newline)}.md")
            write_text(path, TEMPLATE.replace("\n", newline))
            self.assertEqual(generator.main(["--login", "Shino08", "--readme", path]), 0)
            raw = read_bytes(path).decode("utf-8")
            results.append([body.replace("\r\n", "\n") for body in block_bodies(raw)])
        self.assertEqual(results[0], results[1])

    def test_v4_crlf_input_keeps_crlf_and_lf_input_keeps_lf(self):
        for newline, marker in (("\n", "\n"), ("\r\n", "\r\n")):
            path = os.path.join(self._dir.name, f"eol-{len(newline)}.md")
            write_text(path, TEMPLATE.replace("\n", newline))
            generator.main(["--login", "Shino08", "--readme", path])
            raw = read_bytes(path).decode("utf-8")
            self.assertIn(marker, raw)
            self.assertEqual(raw.replace("\r\n", "").count("\n"), 0 if newline == "\r\n" else raw.count("\n"))

    # ------------------------------------------------------------------ V5
    def test_v5_prose_outside_the_markers_survives_verbatim(self):
        write_text(self.readme, TEMPLATE)
        self.run_generator()
        after = read_bytes(self.readme).decode("utf-8")
        self.assertEqual(
            outside_digest(TEMPLATE),
            outside_digest(after),
        )

    def test_v5_repo_list_excludes_profile_repo_forks_and_archived(self):
        rendered = generator.render_repos([dict(item) for item in API_REPOS], "Shino08")
        self.assertNotIn("Shino08/Shino08", rendered)
        self.assertNotIn("forked", rendered)
        self.assertNotIn("archived", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("beta", rendered)

    def test_v5_repo_list_is_ordered_by_most_recent_push(self):
        rendered = generator.render_repos([dict(item) for item in API_REPOS], "Shino08")
        names = [
            line.split("[")[1].split("]")[0]
            for line in rendered.splitlines()
            if line.startswith("| **[")
        ]
        self.assertEqual(names, ["alpha", "beta"])

    def test_v5_repository_order_survives_a_generator_run(self):
        write_text(self.readme, TEMPLATE)
        self.run_generator()
        body = block_bodies(read_bytes(self.readme).decode("utf-8"))[1]
        names = [
            line.split("[")[1].split("]")[0]
            for line in body.splitlines()
            if line.startswith("| **[")
        ]
        self.assertEqual(names, ["alpha", "beta"])


class CommandLineContractTests(unittest.TestCase):
    """Exercise the real command line entry point, including its exit code.

    The GitHub API is replaced by a stub installed through ``sitecustomize``
    so the test stays hermetic and never touches the network.
    """

    STUB = """
import json
import urllib.request

USER = {"login": "Shino08", "public_repos": 5, "followers": 3}
REPOS = [
    {"name": "Shino08", "html_url": "https://github.com/Shino08/Shino08",
     "description": "profile", "language": "HTML", "stargazers_count": 99,
     "pushed_at": "2026-11-01T00:00:00Z", "fork": False, "archived": False},
    {"name": "alpha", "html_url": "https://github.com/Shino08/alpha",
     "description": "keep", "language": "TypeScript", "stargazers_count": 3,
     "pushed_at": "2026-10-02T00:00:00Z", "fork": False, "archived": False},
]


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(request, timeout=None):
    url = getattr(request, "full_url", str(request))
    return _Response(REPOS if "/repos" in url else USER)


urllib.request.urlopen = _urlopen
"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.stub_dir = os.path.join(self._dir.name, "stub")
        os.makedirs(self.stub_dir)
        with io.open(os.path.join(self.stub_dir, "sitecustomize.py"), "w", encoding="utf-8") as handle:
            handle.write(self.STUB)
        self.readme = os.path.join(self._dir.name, "README.md")
        write_text(self.readme, TEMPLATE)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = self.stub_dir
        self.env["BLOG_FEED_URL"] = ""

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--readme", self.readme, "--login", "Shino08", *extra],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_v3_command_exits_non_zero_and_keeps_the_file_untouched(self):
        broken = TEMPLATE.replace(generator.BLOG_LIST_START, "")
        write_text(self.readme, broken)
        before = read_bytes(self.readme)
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error:", result.stderr)
        self.assertIn(generator.BLOG_LIST_START, result.stderr)
        self.assertEqual(before, read_bytes(self.readme))

    def test_v2_command_reports_already_up_to_date_on_the_second_run(self):
        first = self.run_cli()
        self.assertEqual(first.returncode, 0)
        digest = sha256(read_bytes(self.readme))
        second = self.run_cli()
        self.assertEqual(second.returncode, 0)
        self.assertIn("already up to date", second.stdout.lower())
        self.assertEqual(digest, sha256(read_bytes(self.readme)))

    def test_dry_run_reports_the_result_without_writing(self):
        digest = sha256(read_bytes(self.readme))
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("dry run", result.stdout.lower())
        self.assertEqual(digest, sha256(read_bytes(self.readme)))


class WorkflowContractTests(unittest.TestCase):
    """V6: the workflow must keep its safety properties."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_v6_third_party_action_is_pinned_to_a_full_commit_sha(self):
        refs = re.findall(r"uses:\s*\S+@(\S+)", self.text)
        self.assertTrue(refs, "the workflow must use at least one action")
        for ref in refs:
            self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{ref} is not a 40-hex commit SHA")

    def test_v6_permissions_are_scoped_to_contents_write(self):
        block = re.search(r"^permissions:\n((?:[ \t]+.*\n)+)", self.text, re.M)
        self.assertIsNotNone(block)
        self.assertIn("contents: write", block.group(1))
        self.assertNotIn("write-all", self.text)

    def test_v6_a_concurrency_group_exists(self):
        self.assertRegex(self.text, r"(?m)^concurrency:\s*\n\s+group:\s*\S+")

    def test_v6_the_test_step_runs_before_the_generator_step(self):
        # Split the job into step blocks so each name is paired with its own body.
        blocks = re.split(r"(?m)^\s+- name: ", self.text)[1:]
        steps = [(block.splitlines()[0], block) for block in blocks]
        test_index = next(i for i, (name, body) in enumerate(steps) if "unittest" in body)
        generator_index = next(i for i, (name, body) in enumerate(steps) if "update_readme.py" in body)
        self.assertLess(test_index, generator_index)

    def test_v6_the_workflow_cannot_recurse_into_itself(self):
        self.assertNotRegex(self.text, r"(?m)^  push:")
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", self.text)

    def test_v6_the_workflow_has_a_daily_schedule_and_a_manual_trigger(self):
        self.assertRegex(self.text, r"(?m)^\s+- cron: \S+")
        self.assertIn("workflow_dispatch", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
