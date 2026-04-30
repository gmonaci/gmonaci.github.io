#!/usr/bin/env python3
"""
update_pubs.py
==============
Re-generates the publications section in index.html from cv.pdf.

Usage:
    python update_pubs.py              # uses cv.pdf and index.html in same dir
    python update_pubs.py --cv path/to/cv.pdf

Only the block between
    <!-- PUBLICATIONS_START -->
    <!-- PUBLICATIONS_END -->
is replaced; everything else (intro text, styling, etc.) is preserved.
"""

import re
import sys
import argparse
from pathlib import Path
from collections import defaultdict

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber is required.  Run:  pip install pdfplumber")


# ---------------------------------------------------------------------------
# Encoding fixes
# ---------------------------------------------------------------------------
ENCODING_FIXES = [
    ("Su¨sstrunk", "Süsstrunk"), ("Bu¨lent", "Bülent"),
    ("´e", "é"), ("´E", "É"), ("`e", "è"), ("^e", "ê"),
    ("´a", "á"), ("`a", "à"), ("´i", "í"), ("´o", "ó"), ("´u", "ú"),
    ("¨u", "ü"), ("¨o", "ö"), ("¨a", "ä"), ("¸c", "ç"), ("˜n", "ñ"),
    ("Herv´e", "Hervé"), ("D´ejean", "Déjean"), ("St´ephane", "Stéphane"),
    ("Fr´ed´eric", "Frédéric"), ("R´emi", "Rémi"), ("O`scar", "Óscar"),
]

BADGE_PATTERNS = [
    "Oral", "Spotlight", "Highlight", "Best Paper Finalist", "Best Student Paper",
]

AUTHOR_HIGHLIGHT = "Gianluca Monaci"


def fix_encoding(text: str) -> str:
    for old, new in ENCODING_FIXES:
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------
def extract_pub_urls(pdf_path: Path) -> list[str]:
    """Extract paper URLs in reading order from the publications pages."""
    urls = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num in [1, 2, 3]:  # pages 2, 3, 4 (0-indexed)
            if page_num >= len(pdf.pages):
                break
            annots = pdf.pages[page_num].annots or []
            pub_annots = [
                a for a in annots
                if a.get("uri")
                and "patents.google.com" not in a["uri"]
                and "mailto:" not in a["uri"]
                and "linkedin.com" not in a["uri"]
            ]
            for a in sorted(pub_annots, key=lambda x: x["top"]):
                urls.append(a["uri"])
    return urls


def extract_publications(pdf_path: Path) -> list[dict]:
    with pdfplumber.open(pdf_path) as pdf:
        raw = "\n".join(p.extract_text() or "" for p in pdf.pages)

    text = fix_encoding(raw)
    match = re.search(
        r"PUBLICATIONS\n(.*?)(?:PATENTS AND PATENT APPLICATIONS|$)",
        text, re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find PUBLICATIONS section in PDF.")

    lines = [l.strip() for l in match.group(1).splitlines() if l.strip()]
    year_re = re.compile(r"\b(19|20)\d{2}$")
    publications, current = [], []

    for line in lines:
        current.append(line)
        if year_re.search(line):
            if len(current) >= 2:
                title_raw = current[0]
                badge = None
                for b in BADGE_PATTERNS:
                    pat = re.compile(rf"\s*\({re.escape(b)}\)")
                    if pat.search(title_raw):
                        title_raw = pat.sub("", title_raw).strip()
                        badge = b
                        break
                year_match = re.search(r"\b((19|20)\d{2})$", current[-1])
                publications.append(dict(
                    title=title_raw,
                    authors=" ".join(current[1:-1]),
                    venue=current[-1],
                    year=int(year_match.group(1)) if year_match else 0,
                    badge=badge,
                ))
            current = []

    return publications


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def _badge_html(badge: str | None) -> str:
    return f'<span class="badge">{badge}</span>' if badge else ""


def _bold_author(authors: str) -> str:
    return authors.replace(
        AUTHOR_HIGHLIGHT, f"<strong>{AUTHOR_HIGHLIGHT}</strong>"
    )


HIDE_BEFORE_YEAR  = 2015   # years strictly before this go under the dropdown
THUMB_FROM_YEAR   = 2016   # years from this onwards get a figure thumbnail


def _pub_html(idx: int, year: int, p: dict, url: str, indent: str) -> list[str]:
    """Return lines of HTML for a single publication card."""
    i = indent
    has_thumb = year >= THUMB_FROM_YEAR
    fig = f"figures/pub_{idx:02d}.jpg"
    body_lines = [
        f'{i}    <div class="pub-title">'
        f'<a href="{url}" target="_blank" rel="noopener">'
        f'{p["title"]}</a>'
        f'{_badge_html(p["badge"])}</div>',
        f'{i}    <div class="pub-authors">{_bold_author(p["authors"])}</div>',
        f'{i}    <div class="pub-venue">{p["venue"]}</div>',
    ]
    if has_thumb:
        return [
            f'{i}  <div class="pub pub-has-thumb">',
            f'{i}    <a class="pub-thumb" href="{url}" target="_blank" rel="noopener">',
            f'{i}      <img src="{fig}" alt="" loading="lazy" width="140" height="90">',
            f'{i}    </a>',
            f'{i}    <div class="pub-body">',
        ] + [f'  {ln}' for ln in body_lines] + [
            f'{i}    </div>',
            f'{i}  </div>',
        ]
    else:
        return [f'{i}  <div class="pub">'] + body_lines + [f'{i}  </div>']


def _year_group_html(year: int, entries: list, indent: str = "    ") -> list[str]:
    """Return lines of HTML for a single year group.
    entries = [(global_idx, pub_dict, url), ...]
    """
    i = indent
    parts = [
        f'{i}<div class="year-group">',
        f'{i}  <div class="year-label">{year}</div>',
    ]
    for idx, p, url in entries:
        parts += _pub_html(idx, year, p, url, i)
    parts.append(f'{i}</div>')
    return parts


def render_publications_html(publications: list[dict], urls: list[str]) -> str:
    n = min(len(publications), len(urls))
    groups: dict[int, list] = defaultdict(list)
    for i in range(n):
        groups[publications[i]["year"]].append((i + 1, publications[i], urls[i]))
    for i in range(n, len(publications)):
        groups[publications[i]["year"]].append((i + 1, publications[i], "#"))

    recent_years = sorted((y for y in groups if y >= HIDE_BEFORE_YEAR), reverse=True)
    old_years    = sorted((y for y in groups if y <  HIDE_BEFORE_YEAR), reverse=True)

    parts = []

    # Recent publications — shown by default
    for year in recent_years:
        parts += _year_group_html(year, groups[year])

    # Older publications — hidden under a <details> toggle
    if old_years:
        old_count = sum(len(groups[y]) for y in old_years)
        parts.append(f'    <details class="older-pubs">')
        parts.append(
            f'      <summary class="older-pubs-toggle">'
            f'Earlier publications ({old_count})</summary>'
        )
        for year in old_years:
            parts += _year_group_html(year, groups[year], indent="      ")
        parts.append("    </details>")

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# index.html injection
# ---------------------------------------------------------------------------
START_MARKER = "<!-- PUBLICATIONS_START -->"
END_MARKER   = "<!-- PUBLICATIONS_END -->"


def inject_into_html(html_path: Path, pub_html: str) -> None:
    content = html_path.read_text(encoding="utf-8")
    start = content.find(START_MARKER)
    end   = content.find(END_MARKER)
    if start == -1 or end == -1:
        raise ValueError(
            f"Markers not found in {html_path}.\n"
            f"Expected:  {START_MARKER}  and  {END_MARKER}"
        )
    new_content = (
        content[: start + len(START_MARKER)]
        + "\n"
        + pub_html
        + "    "
        + content[end:]
    )
    html_path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update publications in index.html from cv.pdf"
    )
    parser.add_argument("--cv",   default="cv.pdf",    help="Path to the CV PDF")
    parser.add_argument("--html", default="index.html", help="Path to index.html")
    args = parser.parse_args()

    cv_path   = Path(args.cv)
    html_path = Path(args.html)

    if not cv_path.exists():
        sys.exit(f"CV not found: {cv_path}")
    if not html_path.exists():
        sys.exit(f"HTML file not found: {html_path}")

    print(f"Parsing {cv_path} …")
    pubs = extract_publications(cv_path)
    urls = extract_pub_urls(cv_path)
    print(f"  Found {len(pubs)} publications, {len(urls)} URLs.")

    pub_html = render_publications_html(pubs, urls)

    print(f"Updating {html_path} …")
    inject_into_html(html_path, pub_html)
    print("Done.")


if __name__ == "__main__":
    main()
