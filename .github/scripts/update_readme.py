#!/usr/bin/env python3
"""Refresh the auto-generated blocks of a GitHub profile README.

The README is treated as a template: only the text between the paired HTML
comment markers is regenerated. Everything outside the markers is preserved
byte-for-byte, so hand-written sections can never be lost by this script.

The script is fail-safe by design:

* It builds every block in memory and writes the file only once, at the end.
* A missing or duplicated marker aborts the run instead of guessing.
* It never writes a generation timestamp, so re-running it with unchanged
  data produces a byte-identical file and therefore an empty commit.

Usage:
    python3 .github/scripts/update_readme.py [--login USER] [--readme PATH]
                                             [--dry-run]

Environment:
    GITHUB_TOKEN    Token used for the GitHub REST API. Optional for public
                    data, but recommended to avoid the anonymous rate limit.
    BLOG_FEED_URL   Optional RSS/Atom feed rendered in the blog block. When
                    unset, the blog block keeps a short placeholder.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

API_ROOT = "https://api.github.com"
USER_AGENT = "profile-readme-updater"

AUTO_STATS_START = "<!-- AUTO-STATS-START -->"
AUTO_STATS_END = "<!-- AUTO-STATS-END -->"
AUTO_REPOS_START = "<!-- AUTO-REPOS-START -->"
AUTO_REPOS_END = "<!-- AUTO-REPOS-END -->"
BLOG_LIST_START = "<!-- BLOG-LIST-START -->"
BLOG_LIST_END = "<!-- BLOG-LIST-END -->"

REPO_PAGE_SIZE = 100
REPO_MAX_PAGES = 3
DEFAULT_REPO_LIMIT = 6
DEFAULT_BLOG_LIMIT = 5
MAX_DESCRIPTION_LENGTH = 120

# Badge styling mirrors the hand-written sections of the profile README.
BADGE_STYLE = "for-the-badge"
BADGE_BACKGROUND = "0F172A"

# shields.io simple-icons slug and brand colour for the most common languages.
LANGUAGE_BADGES = {
    "JavaScript": ("javascript", "F7DF1E"),
    "TypeScript": ("typescript", "3178C6"),
    "Python": ("python", "3776AB"),
    "PHP": ("php", "777BB4"),
    "Kotlin": ("kotlin", "7F52FF"),
    "Java": ("openjdk", "FFFFFF"),
    "C#": ("dotnet", "512BD4"),
    "C++": ("cplusplus", "00599C"),
    "Go": ("go", "00ADD8"),
    "Rust": ("rust", "FFFFFF"),
    "Astro": ("astro", "BC52EE"),
    "HTML": ("html5", "E34F26"),
    "CSS": ("css3", "1572B6"),
    "Dockerfile": ("docker", "2496ED"),
    "Shell": ("gnubash", "4EAA25"),
    "Vue": ("vuedotjs", "4FC08D"),
    "Svelte": ("svelte", "FF3E00"),
    "Ruby": ("ruby", "CC342D"),
    "Lua": ("lua", "2C2D72"),
}


class UpdateError(RuntimeError):
    """Raised when the README cannot be updated safely."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _http_get(url: str, token: str = "", timeout: int = 30) -> str:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise UpdateError(f"HTTP {error.code} while fetching {url}") from error
    except urllib.error.URLError as error:
        raise UpdateError(f"Network error while fetching {url}: {error.reason}") from error


def fetch_json(url: str, token: str = "") -> object:
    payload = _http_get(url, token)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise UpdateError(f"Invalid JSON response from {url}") from error


def fetch_user(login: str, token: str = "") -> dict:
    url = f"{API_ROOT}/users/{urllib.parse.quote(login)}"
    data = fetch_json(url, token)
    if not isinstance(data, dict):
        raise UpdateError(f"Unexpected payload for user {login}")
    return data


def fetch_repos(login: str, token: str = "") -> list:
    repos: list = []
    quoted = urllib.parse.quote(login)
    for page in range(1, REPO_MAX_PAGES + 1):
        url = (
            f"{API_ROOT}/users/{quoted}/repos"
            f"?type=owner&sort=pushed&per_page={REPO_PAGE_SIZE}&page={page}"
        )
        batch = fetch_json(url, token)
        if not isinstance(batch, list):
            raise UpdateError(f"Unexpected payload for repositories of {login}")
        repos.extend(batch)
        if len(batch) < REPO_PAGE_SIZE:
            break
    return repos


def fetch_feed(url: str, timeout: int = 30) -> list:
    """Fetch and parse an RSS 2.0 or Atom feed into a list of entries."""
    return parse_feed(_http_get(url, timeout=timeout))


def parse_feed(body: str) -> list:
    """Parse an RSS 2.0 or Atom document into a list of entries."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise UpdateError(f"The configured feed is not valid XML: {error}") from error

    entries: list = []
    for node in root.iter():
        if _local_name(node.tag) not in ("item", "entry"):
            continue

        title = None
        published = None
        links: list = []
        for child in node:
            name = _local_name(child.tag)
            if name == "title" and title is None:
                title = _clean_text(child)
            elif name in ("pubDate", "published", "updated") and published is None:
                published = _clean_text(child)
            elif name == "link":
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")
                if href:
                    links.append((rel, href))
                else:
                    text = _clean_text(child)
                    if text:
                        links.append(("alternate", text))

        if not title:
            continue

        link = _pick_link(links)
        if not link:
            continue

        display_date, sort_key = _parse_date(published)
        entries.append(
            {
                "title": title,
                "link": link,
                "date": display_date,
                "sort_key": sort_key,
            }
        )

    entries.sort(key=lambda entry: entry["sort_key"], reverse=True)
    return entries


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _clean_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split()).strip()


def _pick_link(links: list) -> str:
    for rel, href in links:
        if rel == "alternate":
            return href
    for rel, href in links:
        if rel not in ("self", "hub", "replies", "edit"):
            return href
    return ""


def _parse_date(raw: str) -> tuple:
    """Return a (display date, sort key) pair for a feed date.

    The display date is day-precision for humans, while the sort key keeps
    the full timestamp so entries published on the same day still order
    newest first.
    """
    if not raw:
        return "", ""
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return "", ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d"), parsed.isoformat()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def language_breakdown(repos: list) -> list:
    counter: Counter = Counter()
    for repo in repos:
        if repo.get("fork") or repo.get("archived"):
            continue
        language = repo.get("language")
        if language:
            counter[language] += 1
    return counter.most_common()


def _badge_text(value) -> str:
    """Escape one shields.io static-badge path segment.

    A single ``-`` separates the segments, so a literal dash is doubled and
    spaces become underscores, which is what shields.io expects.
    """
    text = str(value).replace("-", "--").replace("_", "__")
    return urllib.parse.quote(text.replace(" ", "_"), safe="")


def _badge(label: str, value, logo: str = "", color: str = "FFFFFF") -> str:
    path = f"{_badge_text(label)}-{_badge_text(value)}-{BADGE_BACKGROUND}"
    query = f"?style={BADGE_STYLE}"
    if logo:
        query += f"&logo={logo}&logoColor={color}"
    return f'<img alt="{label}" src="https://img.shields.io/badge/{path}{query}" />'


def _html(value) -> str:
    """Escape a value for use inside an HTML table cell."""
    return html.escape(" ".join(str(value).split()), quote=True)


def _shield(slug: str, kind: str, alt: str) -> str:
    """A live shields.io badge, so the number never needs a commit."""
    return (
        f'<img alt="{alt}" src="https://img.shields.io/github/{kind}/{slug}'
        '?style=flat-square&labelColor=343b41" />'
    )


def render_stats(user: dict, repos: list) -> str:
    public_repos = user.get("public_repos")
    if not isinstance(public_repos, int):
        public_repos = len(repos)
    followers = user.get("followers", 0)
    total_stars = sum(repo.get("stargazers_count", 0) for repo in repos)
    languages = language_breakdown(repos)

    badges = [
        _badge("Repositorios", public_repos, "github", "FFFFFF"),
        _badge("Estrellas", total_stars, "github", "FCC624"),
        _badge("Seguidores", followers, "github", "38BDF8"),
    ]
    if languages:
        primary, primary_count = languages[0]
        logo, color = LANGUAGE_BADGES.get(primary, ("", "FFFFFF"))
        badges.append(_badge("Principal", f"{primary} ({primary_count})", logo, color))
    return '<p align="center">\n  ' + "\n  ".join(badges) + "\n</p>"


def render_repos(repos: list, login: str, limit: int = DEFAULT_REPO_LIMIT) -> str:
    candidates = [
        repo
        for repo in repos
        if str(repo.get("name", "")).lower() != login.lower()
        and not repo.get("fork")
        and not repo.get("archived")
    ]
    candidates.sort(key=lambda repo: repo.get("pushed_at") or "", reverse=True)

    rows: list = []
    for repo in candidates[:limit]:
        name = repo.get("name", "")
        url = repo.get("html_url", f"https://github.com/{login}/{name}")
        slug = f"{login}/{name}"

        description = " ".join((repo.get("description") or "").split())
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[: MAX_DESCRIPTION_LENGTH - 3].rstrip() + "..."

        label = f"<b>{_html(name)}</b>"
        if description:
            label += f"<br><i>{_html(description)}</i>"

        language = _html(repo.get("language") or "")
        stack = f"<code>{language}</code>" if language else "&mdash;"
        pushed_at = (repo.get("pushed_at") or "")[:10] or "&mdash;"

        rows.append(
            "    <tr>\n"
            f'      <td><a href="{_html(url)}">{label}</a></td>\n'
            f"      <td>{stack}</td>\n"
            f'      <td>{_shield(slug, "stars", "Stars")}</td>\n'
            f'      <td>{_shield(slug, "forks", "Forks")}</td>\n'
            f'      <td>{_shield(slug, "issues", "Issues")}</td>\n'
            f'      <td>{_shield(slug, "issues-pr", "Pull requests")}</td>\n'
            f"      <td>{pushed_at}</td>\n"
            "    </tr>"
        )

    if not rows:
        return "_No hay repositorios públicos para mostrar._"

    header = [
        "<table>",
        '  <thead align="center">',
        "    <tr>",
        "      <td><b>Proyecto</b></td>",
        "      <td><b>Stack</b></td>",
        "      <td><b>⭐ Stars</b></td>",
        "      <td><b>Forks</b></td>",
        "      <td><b>Issues</b></td>",
        "      <td><b>PRs</b></td>",
        "      <td><b>Actualizado</b></td>",
        "    </tr>",
        "  </thead>",
        "  <tbody>",
    ]
    return "\n".join(header + rows + ["  </tbody>", "</table>"])


def render_blog(entries) -> str:
    if entries is None:
        return (
            "> 💡 **Sin feed configurado.** Añade la variable de repositorio "
            "`BLOG_FEED_URL` en **Settings → Secrets and variables → Actions → "
            "Variables** y tus últimos artículos aparecerán aquí automáticamente."
        )
    if not entries:
        return "_El feed RSS configurado no contiene entradas._"

    lines: list = []
    for entry in entries:
        title = entry["title"].replace("[", "\\[").replace("]", "\\]")
        line = f"- [{title}]({entry['link']})"
        if entry["date"]:
            line += f" — {entry['date']}"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# README templating
# --------------------------------------------------------------------------- #


def replace_section(
    text: str,
    start_marker: str,
    end_marker: str,
    content: str,
    newline: str = "\n",
) -> str:
    starts = [match.start() for match in re.finditer(re.escape(start_marker), text)]
    ends = [match.start() for match in re.finditer(re.escape(end_marker), text)]

    if len(starts) != 1:
        raise UpdateError(
            f"Expected exactly one {start_marker} marker, found {len(starts)}"
        )
    if len(ends) != 1:
        raise UpdateError(
            f"Expected exactly one {end_marker} marker, found {len(ends)}"
        )
    if starts[0] >= ends[0]:
        raise UpdateError(f"{start_marker} must appear before {end_marker}")

    body = content.strip("\n").replace("\n", newline)
    head = text[: starts[0] + len(start_marker)]
    tail = text[ends[0] :]
    return f"{head}{newline}{body}{newline}{tail}"


def read_readme(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except OSError as error:
        raise UpdateError(f"Cannot read {path}: {error}") from error


def write_readme(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle_fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".readme-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--login",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER")
        or os.environ.get("GITHUB_USER")
        or "",
        help="GitHub login that owns the profile repository",
    )
    parser.add_argument(
        "--readme",
        default="README.md",
        help="Path to the README file to update",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the result without writing the file",
    )
    args = parser.parse_args(argv)

    login = args.login.strip()
    if not login:
        parser.error("A GitHub login is required (--login or GITHUB_REPOSITORY_OWNER)")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    feed_url = os.environ.get("BLOG_FEED_URL", "").strip()

    user = fetch_user(login, token)
    repos = fetch_repos(login, token)

    stats_block = render_stats(user, repos)
    repos_block = render_repos(repos, login)
    if feed_url:
        blog_block = render_blog(fetch_feed(feed_url))
    else:
        blog_block = render_blog(None)

    original = read_readme(args.readme)
    newline = "\r\n" if "\r\n" in original else "\n"

    updated = original
    sections = (
        (AUTO_STATS_START, AUTO_STATS_END, stats_block),
        (AUTO_REPOS_START, AUTO_REPOS_END, repos_block),
        (BLOG_LIST_START, BLOG_LIST_END, blog_block),
    )
    for start_marker, end_marker, content in sections:
        updated = replace_section(
            updated, start_marker, end_marker, content, newline=newline
        )

    if updated == original:
        print("README is already up to date.")
        return 0

    if args.dry_run:
        print("Dry run: README would be updated.")
        print(updated)
        return 0

    write_readme(args.readme, updated)
    print("README updated.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
