"""Links in the pages the archive ships resolve inside the archive.

The archive carries only build/package.py's SOURCES. A relative link from a shipped page to
a repository-only path (CLAUDE.md, handoff/, build/, images/) works on GitHub and breaks at
a site, so shipped pages link there by https URL instead.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "lib"))

import package  # noqa: E402

FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1", re.S | re.M)
CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def shipped(rel: Path) -> bool:
    parts = rel.parts
    if package.SKIP_DIRS.intersection(parts) or any(p.startswith(".") for p in parts):
        return False
    if rel.name.endswith(package.SKIP_SUFFIXES):
        return False
    s = rel.as_posix()
    return any(s == src or s.startswith(src + "/") for src in package.SOURCES)


def shipped_pages():
    pages = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT)
        if rel.parts[0].startswith(".") or not shipped(rel):
            continue
        pages.append(rel)
    return pages


def links(text: str):
    """Relative link targets, without their #anchor."""
    for target in targets(text):
        if not target.startswith("#"):
            yield target.split("#", 1)[0]


def targets(text: str):
    """Every link target outside code that isn't a URL, with its #anchor."""
    text = CODE.sub("", FENCE.sub("", text))
    for m in LINK.finditer(text):
        target = m.group(1)
        if not re.match(r"[a-z][a-z0-9+.-]*:", target):
            yield target


def anchors(text: str):
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens, -N for repeats."""
    seen, out = {}, set()
    for line in FENCE.sub("", text).splitlines():
        m = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        slug = re.sub(r"[^\w\- ]", "", m.group(1).lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def test_there_are_shipped_pages():
    pages = {p.as_posix() for p in shipped_pages()}
    assert {"README.md", "docs/README.md", "docs/CONFIG.md"} <= pages


@pytest.mark.parametrize("page", shipped_pages(), ids=lambda p: p.as_posix())
def test_relative_links_stay_inside_the_archive(page):
    bad = []
    for target in links((ROOT / page).read_text()):
        resolved = (ROOT / page).parent / target
        try:
            rel = resolved.resolve().relative_to(ROOT)
        except ValueError:
            bad.append(f"{target} (outside the repository)")
            continue
        if not resolved.exists():
            bad.append(f"{target} (missing)")
        elif not shipped(rel):
            bad.append(f"{target} (not in the archive: link to https://github.com/TrevorBenson/dciab/blob/main/{rel.as_posix()})")
    assert not bad, f"{page}: " + "; ".join(bad)


@pytest.mark.parametrize("page", shipped_pages(), ids=lambda p: p.as_posix())
def test_anchors_name_real_headings(page):
    bad = []
    for target in targets((ROOT / page).read_text()):
        if "#" not in target:
            continue
        path, anchor = target.split("#", 1)
        doc = (ROOT / page).parent / path if path else ROOT / page
        if doc.suffix == ".md" and doc.is_file() and anchor not in anchors(doc.read_text()):
            bad.append(target)
    assert not bad, f"{page}: no such heading for " + ", ".join(bad)


def test_anchors_follow_github_rules():
    text = "# `dciab mgmt`\n## 4. Recipients and their keys\n## What MGMT needs from INFRA: nothing\n## Steps\n## Steps\n"
    assert anchors(text) == {"dciab-mgmt", "4-recipients-and-their-keys",
                             "what-mgmt-needs-from-infra-nothing", "steps", "steps-1"}


def test_the_check_finds_a_repository_only_link(tmp_path):
    text = ("See [rules](../CLAUDE.md), `[not](a-link)`, [ok](CONFIG.md#top) and [web](https://example.net).\n"
            "1. A step:\n   ```console\n   $ see [not](a-link-either)\n   ```\n")
    assert list(links(text)) == ["../CLAUDE.md", "CONFIG.md"]
    assert not shipped(Path("CLAUDE.md")) and not shipped(Path("handoff/README.md"))
    assert shipped(Path("docs/README.md")) and not shipped(Path("lib/x/tests/README.md"))
    assert not shipped(Path("docs/.draft.md")) and not shipped(Path("docs/notes.md~"))
