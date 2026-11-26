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

FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def shipped(rel: Path) -> bool:
    parts = rel.parts
    if package.SKIP_DIRS.intersection(parts):
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
    text = CODE.sub("", FENCE.sub("", text))
    for m in LINK.finditer(text):
        target = m.group(1)
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        yield target.split("#", 1)[0]


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


def test_the_check_finds_a_repository_only_link(tmp_path):
    text = "See [rules](../CLAUDE.md), `[not](a-link)`, [ok](CONFIG.md#top) and [web](https://example.net).\n"
    assert list(links(text)) == ["../CLAUDE.md", "CONFIG.md"]
    assert not shipped(Path("CLAUDE.md")) and not shipped(Path("handoff/README.md"))
    assert shipped(Path("docs/README.md")) and not shipped(Path("lib/x/tests/README.md"))
