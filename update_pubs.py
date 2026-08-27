#!/usr/bin/env python3
"""
update_pubs.py
==============
Re-generates the publications section in index.html from cv.pdf.

Usage:
    python update_pubs.py                    # uses cv.pdf and index.html in same dir
    python update_pubs.py --cv path/to/cv.pdf
    python update_pubs.py --check            # parse + report only, write nothing
    python update_pubs.py --strict           # exit non-zero if the parse looks wrong

Only the block between
    <!-- PUBLICATIONS_START -->
    <!-- PUBLICATIONS_END -->
is replaced; everything else (intro text, styling, etc.) is preserved.

How the parsing works
---------------------
Every publication in the CV has a hyperlinked title, so the PDF's link
annotations are used as the ground truth for "a new record starts here".
Line geometry (page + vertical span) is matched against annotation geometry.

This is deliberately independent of the *textual* layout of the CV: it does not
care whether venues end in a year, whether years are printed as group headings,
or how many lines the authors wrap onto. If the link annotations ever disappear
(e.g. the CV is exported without hyperlinks), a heuristic fallback parser kicks
in automatically and a warning is printed.

Run with --check after any CV restyling to see exactly what was parsed.
"""

from __future__ import annotations

import re
import sys
import json
import html
import difflib
import argparse
import unicodedata
from pathlib import Path
from collections import defaultdict

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber is required.  Run:  pip install pdfplumber")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECTION_HEADING = "PUBLICATIONS"

# Titles ending in one of these parentheticals get a badge instead.
BADGE_PATTERNS = [
    "Oral", "Spotlight", "Highlight", "Best Paper Finalist",
    "Best Student Paper", "Best Paper Award", "Best Paper",
    "Honorable Mention", "Outstanding Paper",
]

AUTHOR_HIGHLIGHT = "Gianluca Monaci"

HIDE_BEFORE_YEAR = 2015   # years strictly before this go under the dropdown
THUMB_FROM_YEAR  = 2016   # years from this onwards get a figure thumbnail

# URLs that are never publication links.
NON_PUB_URL_HINTS = ("patents.google.com", "mailto:", "linkedin.com",
                     "scholar.google", "github.com/")

# A line that looks like a venue. Used only for validation warnings.
VENUE_HINT = re.compile(
    r"(19|20)\d{2}\b|arxiv|conference|workshop|symposium|transactions?\b"
    r"|journal|proceedings|letters|\bpages\b|\bvol\.?\b|\bin proc",
    re.I,
)

# Year group headings: "2026", "2014 and earlier", "Before 2015", "2010-2014".
YEAR_HEADING_RES = [
    (re.compile(r"^((?:19|20)\d{2})(?:\s+(?:and|or)\s+(?:earlier|before))?$", re.I), 0),
    (re.compile(r"^(?:before|prior\s+to)\s+((?:19|20)\d{2})$", re.I), -1),
    (re.compile(r"^(?:19|20)\d{2}\s*[-–—]\s*((?:19|20)\d{2})$"), 0),
]

# A section heading in the CV (all caps). Marks the end of PUBLICATIONS.
SECTION_HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 &,'’/\-]{3,}$")


# ---------------------------------------------------------------------------
# Text repair
# ---------------------------------------------------------------------------
# Names the generic accent logic below cannot get right (the PDF encodes the
# accent ambiguously). Applied verbatim, before anything else.
SPECIAL_FIXES = {
    "O`scar": "Óscar",
}

# Spacing accent -> combining mark.
#
# Deliberately excludes the ASCII apostrophe, caret and tilde: they occur in
# ordinary text ("it's", "x^2", "~50") far more often than as accents, and
# treating them as accents corrupts more than it fixes. The backtick is kept
# but only fires when glued between two letters (see _ASCII_STRICT).
ACCENTS = {
    "\u00b4": "\u0301",  # ´ acute
    "\u02ca": "\u0301",  # ˊ modifier acute
    "`":      "\u0300",  # ` grave
    "\u02cb": "\u0300",  # ˋ modifier grave
    "\u02c6": "\u0302",  # ˆ circumflex
    "\u00a8": "\u0308",  # ¨ diaeresis
    "\u02dc": "\u0303",  # ˜ tilde
    "\u00b8": "\u0327",  # ¸ cedilla
    "\u02da": "\u030a",  # ˚ ring
}
VOWELS = set("aeiouyAEIOUY")
# Cedilla and ring always bind to the character that follows them.
ACCENTS_BIND_FORWARD = {"\u00b8", "\u02da"}
# These need a letter on *both* sides before they count as an accent.
_ASCII_STRICT = {"`"}

_ACCENT_CLASS = "".join(re.escape(a) for a in ACCENTS)
_ACCENT_RE = re.compile(rf"([A-Za-z])?([{_ACCENT_CLASS}])([A-Za-z])?")


def fix_encoding(text: str) -> str:
    """Repair LaTeX-style accents that pdfplumber extracts as separate glyphs.

    Handles both placements seen in practice:
        Herv´e   -> Hervé    (accent before the letter)
        Bu¨lent  -> Bülent   (accent after the letter)

    The disambiguation rule: an accent binds to the *following* character when
    that character is a vowel (or the accent is a cedilla/ring), otherwise to
    the preceding one.
    """
    for old, new in SPECIAL_FIXES.items():
        text = text.replace(old, new)

    def repl(m: re.Match) -> str:
        before, accent, after = m.group(1), m.group(2), m.group(3)
        mark = ACCENTS[accent]

        # ASCII look-alikes only count as accents when glued between letters.
        if accent in _ASCII_STRICT and not (before and after):
            return m.group(0)

        forward = accent in ACCENTS_BIND_FORWARD
        if not forward:
            if after and after in VOWELS:
                forward = True
            elif before and before in VOWELS:
                forward = False
            elif after:
                forward = True
            else:
                forward = False

        if forward and after:
            return f"{before or ''}{unicodedata.normalize('NFC', after + mark)}"
        if not forward and before:
            return f"{unicodedata.normalize('NFC', before + mark)}{after or ''}"
        return m.group(0)

    prev = None
    while prev != text:                      # a letter can carry two accents
        prev = text
        text = _ACCENT_RE.sub(repl, text)

    return unicodedata.normalize("NFC", text)


# Lowercase name particles that get glued to the previous word.
_PARTICLES = r"(?:van|von|der|den|de|del|della|di|da|dos|du|le|la|ter|ten)"
_NO_SPLIT_PREFIX = re.compile(r"^(?:Mc|Mac|O')", re.I)


def fix_spacing(text: str) -> str:
    """Repair author lists where the PDF lost its spaces.

    e.g.  'Gianluca Monaci,TommasoGritti,MartinevanBeers,AdVermeulen'
       -> 'Gianluca Monaci, Tommaso Gritti, Martine van Beers, Ad Vermeulen'

    Only comma-separated tokens that contain no space at all are de-glued, so
    correctly-extracted names are never touched.
    """
    text = re.sub(r",(?=[^\s,])", ", ", text)          # space after commas

    def deglue(token: str) -> str:
        if " " in token or _NO_SPLIT_PREFIX.match(token):
            return token
        if not re.search(r"[a-z][A-Z]", token):        # no glue evidence
            return token
        token = re.sub(rf"(?<=[a-z]){_PARTICLES}(?=[A-Z])", r" \g<0> ", token)
        token = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)
        return re.sub(r"\s{2,}", " ", token).strip()

    return ", ".join(deglue(t.strip()) for t in text.split(","))


def strip_badge(title: str) -> tuple[str, str | None]:
    """Pull a trailing '(Oral)'-style parenthetical off a title."""
    for badge in BADGE_PATTERNS:
        pat = re.compile(rf"\s*\(\s*{re.escape(badge)}\s*\)\s*$", re.I)
        if pat.search(title):
            return pat.sub("", title).strip(), badge
    return title.strip(), None


def year_heading(text: str) -> int | None:
    """Return the year a group-heading line denotes, or None."""
    for pat, offset in YEAR_HEADING_RES:
        m = pat.match(text.strip())
        if m:
            return int(m.group(1)) + offset
    return None


# ---------------------------------------------------------------------------
# PDF geometry
# ---------------------------------------------------------------------------
def _pdf_lines(pdf) -> list[dict]:
    """Flat list of text lines with page index and vertical span."""
    out = []
    for pi, page in enumerate(pdf.pages):
        for ln in page.extract_text_lines():
            text = ln["text"].strip()
            if text:
                out.append(dict(page=pi, top=ln["top"], bottom=ln["bottom"],
                                x0=ln["x0"], x1=ln["x1"], text=text))
    return out


def _pdf_links(pdf) -> dict[int, list[dict]]:
    """Publication-ish link annotations, keyed by page index."""
    links = defaultdict(list)
    for pi, page in enumerate(pdf.pages):
        for a in (page.annots or []):
            uri = a.get("uri")
            if not uri or any(h in uri for h in NON_PUB_URL_HINTS):
                continue
            links[pi].append(dict(uri=uri, top=a["top"], bottom=a["bottom"],
                                  x0=a["x0"], x1=a["x1"]))
    return links


def _link_for_line(line: dict, links: dict[int, list[dict]]) -> str | None:
    """URI of the annotation covering this line, if any."""
    best, best_overlap = None, 0.0
    for a in links.get(line["page"], []):
        v = min(line["bottom"], a["bottom"]) - max(line["top"], a["top"])
        h = min(line["x1"], a["x1"]) - max(line["x0"], a["x0"])
        if v <= 0 or h <= 0:
            continue
        overlap = v * h
        if overlap > best_overlap:
            best, best_overlap = a["uri"], overlap
    return best


def _section_lines(lines: list[dict]) -> list[dict]:
    """Slice out the PUBLICATIONS section."""
    start = next((i for i, l in enumerate(lines)
                  if l["text"].upper().rstrip(":") == SECTION_HEADING), None)
    if start is None:
        raise ValueError(
            f"Could not find a '{SECTION_HEADING}' heading in the PDF. "
            "If the section was renamed, update SECTION_HEADING."
        )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        t = lines[i]["text"].strip()
        if SECTION_HEADING_RE.match(t) and year_heading(t) is None:
            end = i
            break
    return lines[start + 1:end]


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------
def _finish(title_parts: list[str], body: list[str], url: str | None,
            heading_year: int | None, warnings: list[str]) -> dict | None:
    if not title_parts:
        return None

    title = " ".join(title_parts)
    title = re.sub(r"-\s+(?=[a-z])", "", title)        # de-hyphenate line breaks
    title, badge = strip_badge(fix_encoding(title))

    if not body:
        warnings.append(f"no authors/venue found for: {title[:70]!r}")
        authors, venue = "", ""
    elif len(body) == 1:
        warnings.append(f"only one line under the title (venue? authors?): {title[:70]!r}")
        authors, venue = "", fix_encoding(body[0])
    else:
        venue = fix_encoding(body[-1])
        authors = fix_spacing(fix_encoding(" ".join(body[:-1])))

    if venue and not VENUE_HINT.search(venue):
        warnings.append(f"venue line looks unusual for {title[:50]!r}: {venue[:60]!r}")
    if len(body) > 4:
        warnings.append(f"{len(body)} lines under {title[:50]!r} — title may have "
                        "wrapped without a link on the second line")

    m = re.search(r"\b((?:19|20)\d{2})\b(?!.*\b(?:19|20)\d{2}\b)", venue)
    year = int(m.group(1)) if m else (heading_year or 0)
    if not m and heading_year is None:
        warnings.append(f"no year for: {title[:70]!r}")

    return dict(title=title, authors=authors, venue=venue,
                year=year, badge=badge, url=url or "#")


def _parse_by_links(section: list[dict], links: dict[int, list[dict]],
                    warnings: list[str]) -> list[dict]:
    """Primary parser: a new record starts at every hyperlinked line."""
    pubs: list[dict] = []
    title_parts: list[str] = []
    body: list[str] = []
    url: str | None = None
    heading_year: int | None = None
    prev_was_title = False

    for line in section:
        text = line["text"]

        hy = year_heading(text)
        if hy is not None:
            pub = _finish(title_parts, body, url, heading_year, warnings)
            if pub:
                pubs.append(pub)
            title_parts, body, url = [], [], None
            heading_year = hy
            prev_was_title = False
            continue

        line_url = _link_for_line(line, links)

        if line_url and prev_was_title and line_url == url:
            title_parts.append(text)              # wrapped title, same link
            continue

        if line_url:
            pub = _finish(title_parts, body, url, heading_year, warnings)
            if pub:
                pubs.append(pub)
            title_parts, body, url = [text], [], line_url
            prev_was_title = True
            continue

        if title_parts:
            body.append(text)
        prev_was_title = False

    pub = _finish(title_parts, body, url, heading_year, warnings)
    if pub:
        pubs.append(pub)
    return pubs


def _parse_by_layout(section: list[dict], warnings: list[str]) -> list[dict]:
    """Fallback parser for a CV exported without hyperlinks.

    Assumes the repeating pattern  title / authors… / venue  and uses the venue
    line (year, or a venue keyword) to close each record.
    """
    warnings.append("no link annotations found — using the heuristic fallback parser; "
                    "titles will have no URLs")
    pubs: list[dict] = []
    chunk: list[str] = []
    heading_year: int | None = None

    for line in section:
        text = line["text"]
        hy = year_heading(text)
        if hy is not None:
            if chunk:
                pubs.append(_finish(chunk[:1], chunk[1:], None, heading_year, warnings))
                chunk = []
            heading_year = hy
            continue

        chunk.append(text)
        if len(chunk) >= 3 and VENUE_HINT.search(text):
            pubs.append(_finish(chunk[:1], chunk[1:], None, heading_year, warnings))
            chunk = []

    if chunk:
        pubs.append(_finish(chunk[:1], chunk[1:], None, heading_year, warnings))
    return [p for p in pubs if p]


_CACHE: dict[Path, tuple[list[dict], list[str]]] = {}


def parse_cv(pdf_path: Path) -> tuple[list[dict], list[str]]:
    """Parse the CV once and cache. Returns (publications, warnings)."""
    key = Path(pdf_path).resolve()
    if key in _CACHE:
        return _CACHE[key]

    warnings: list[str] = []
    with pdfplumber.open(key) as pdf:
        lines = _pdf_lines(pdf)
        links = _pdf_links(pdf)

    section = _section_lines(lines)
    if not section:
        raise ValueError("The PUBLICATIONS section is empty.")

    pages = {l["page"] for l in section}
    has_links = any(links.get(p) for p in pages)
    pubs = (_parse_by_links(section, links, warnings) if has_links
            else _parse_by_layout(section, warnings))

    if not pubs:
        raise ValueError("Parsed zero publications from the PUBLICATIONS section.")

    leftovers = {c for p in pubs for f in ("title", "authors", "venue")
                 for c in p[f] if c in ACCENTS}
    if leftovers:
        warnings.append(f"unresolved accent glyphs remain: {sorted(leftovers)}")

    seen: dict[str, int] = {}
    for p in pubs:
        k = p["title"].lower()
        seen[k] = seen.get(k, 0) + 1
    for k, n in seen.items():
        if n > 1:
            warnings.append(f"duplicate title parsed {n}×: {k[:60]!r}")

    _CACHE[key] = (pubs, warnings)
    return pubs, warnings


# ---------------------------------------------------------------------------
# Back-compatible API (fetch_figures.py imports these)
# ---------------------------------------------------------------------------
def extract_publications(pdf_path: Path) -> list[dict]:
    return parse_cv(Path(pdf_path))[0]


def extract_pub_urls(pdf_path: Path) -> list[str]:
    return [p["url"] for p in extract_publications(pdf_path)]


def thumb_index_map(pubs: list[dict]) -> dict[int, int]:
    """DEPRECATED — the old positional numbering (oldest = pub_01).

    Kept only so the one-shot `--migrate` path can reconstruct the legacy
    filenames. Use assign_keys() for anything else.
    """
    eligible = [i for i, p in enumerate(pubs) if p["year"] >= THUMB_FROM_YEAR]
    total = len(eligible)
    return {i: total - pos for pos, i in enumerate(eligible)}


# ---------------------------------------------------------------------------
# Stable thumbnail identity
# ---------------------------------------------------------------------------
# Thumbnails used to be numbered by position (oldest = pub_01), which meant
# reordering the CV, or inserting a paper anywhere but the top, silently
# reassigned every filename after it — and the manual overrides in
# figures/overrides/ would then attach to the wrong papers.
#
# Instead each paper now gets a slug that is minted once and recorded in
# figures/index.json. On every later run a paper is matched back to its
# existing slug by normalised title, with a fuzzy fallback so that fixing a
# typo or dropping a subtitle does not orphan its thumbnail. Order in the CV
# is irrelevant, and nothing ever gets renamed behind your back.

REGISTRY_PATH = "figures/index.json"
SLUG_MAX_LEN = 40
FUZZY_THRESHOLD = 0.80     # normalised-title similarity needed to reuse a slug
CONTAINMENT_THRESHOLD = 0.90   # or: one title's words almost entirely inside the other
CONTAINMENT_MIN_WORDS = 4      # …provided the shorter title is this long


def _similarity(a: str, b: str) -> float:
    """How likely two normalised titles are the same paper.

    Character ratio alone under-scores the common editorial changes — dropping
    a subtitle, adding one — because they shift length a lot while leaving the
    wording intact. So we also measure how completely the shorter title's words
    are contained in the longer one, and take the better of the two.
    """
    ratio = difflib.SequenceMatcher(None, a, b).ratio()

    wa, wb = set(a.split()), set(b.split())
    if wa and wb:
        shorter = min(len(wa), len(wb))
        containment = len(wa & wb) / shorter
        if shorter >= CONTAINMENT_MIN_WORDS and containment >= CONTAINMENT_THRESHOLD:
            return max(ratio, containment)
    return ratio


def normalize_title(title: str) -> str:
    """Fold a title down to what is stable about it: lowercase alphanumerics."""
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"[^a-z0-9]+", " ", folded.lower())
    return " ".join(folded.split())


def slugify(title: str) -> str:
    """Human-readable, filesystem-safe stem for a title."""
    words = normalize_title(title).split()
    slug = ""
    for w in words:
        if slug and len(slug) + 1 + len(w) > SLUG_MAX_LEN:
            break
        slug = f"{slug}-{w}" if slug else w
    return slug or "untitled"


def load_registry(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}). Fix or delete it.")
    return data.get("papers", {})


def save_registry(path: Path, papers: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "Stable slug -> paper identity for figures/pub_<slug>.jpg. "
                    "Generated by update_pubs.py; keep it in git. Deleting an "
                    "entry orphans that paper's thumbnail and override.",
        "papers": papers,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                               sort_keys=True) + "\n", encoding="utf-8")


def _unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def assign_keys(pubs: list[dict], registry_path: Path | str = REGISTRY_PATH,
                write: bool = False,
                warnings: list[str] | None = None) -> dict[int, str]:
    """Position in `pubs` -> stable slug, for thumbnail-eligible papers.

    Matching order: exact normalised title, then best fuzzy match above
    FUZZY_THRESHOLD, then mint a fresh slug.
    """
    warnings = warnings if warnings is not None else []
    path = Path(registry_path)
    papers = load_registry(path)

    eligible = [i for i, p in enumerate(pubs) if p["year"] >= THUMB_FROM_YEAR]

    by_norm = {e["norm"]: slug for slug, e in papers.items() if e.get("norm")}
    claimed: set[str] = set()
    keys: dict[int, str] = {}

    # Pass 1 — exact normalised-title matches.
    unmatched = []
    for i in eligible:
        norm = normalize_title(pubs[i]["title"])
        slug = by_norm.get(norm)
        if slug and slug not in claimed:
            keys[i], _ = slug, claimed.add(slug)
        else:
            unmatched.append(i)

    # Pass 2 — fuzzy match against slugs nothing has claimed yet.
    free = [(slug, e) for slug, e in papers.items()
            if slug not in claimed and e.get("norm")]
    for i in list(unmatched):
        norm = normalize_title(pubs[i]["title"])
        scored = sorted(
            ((_similarity(norm, e["norm"]), slug) for slug, e in free
             if slug not in claimed),
            reverse=True,
        )
        if not scored:
            continue
        best_score, best = scored[0]
        if best_score >= FUZZY_THRESHOLD:
            keys[i] = best
            claimed.add(best)
            unmatched.remove(i)
            if papers[best]["title"] != pubs[i]["title"]:
                warnings.append(
                    f"title changed, keeping thumbnail {best!r} "
                    f"(similarity {best_score:.2f}): {pubs[i]['title'][:60]!r}"
                )
            if len(scored) > 1 and best_score - scored[1][0] < 0.05:
                warnings.append(
                    f"ambiguous thumbnail match for {pubs[i]['title'][:50]!r}: "
                    f"{best!r} ({best_score:.2f}) vs {scored[1][1]!r} "
                    f"({scored[1][0]:.2f}) — check figures/index.json"
                )

    # Pass 3 — mint new slugs.
    taken = set(papers) | set(claimed)
    for i in unmatched:
        slug = _unique_slug(slugify(pubs[i]["title"]), taken)
        taken.add(slug)
        claimed.add(slug)
        keys[i] = slug

    # Refresh stored metadata; keep retired papers so their slug is never reused.
    for i in eligible:
        slug = keys[i]
        p = pubs[i]
        entry = papers.get(slug, {})
        entry.update(title=p["title"], norm=normalize_title(p["title"]),
                     year=p["year"], venue=p["venue"], url=p["url"])
        entry.pop("retired", None)
        papers[slug] = entry
    for slug, entry in papers.items():
        if slug not in claimed:
            entry["retired"] = True

    if write:
        save_registry(path, papers)

    return keys


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _badge_html(badge: str | None) -> str:
    return f'<span class="badge">{_esc(badge)}</span>' if badge else ""


def _bold_author(authors: str) -> str:
    return _esc(authors).replace(
        _esc(AUTHOR_HIGHLIGHT), f"<strong>{_esc(AUTHOR_HIGHLIGHT)}</strong>"
    )


def _pub_html(key: str | None, year: int, p: dict, indent: str) -> list[str]:
    i = indent
    url = _esc(p["url"])
    body_lines = [
        f'{i}    <div class="pub-title">'
        f'<a href="{url}" target="_blank" rel="noopener">'
        f'{_esc(p["title"])}</a>'
        f'{_badge_html(p["badge"])}</div>',
        f'{i}    <div class="pub-authors">{_bold_author(p["authors"])}</div>',
        f'{i}    <div class="pub-venue">{_esc(p["venue"])}</div>',
    ]
    if key and year >= THUMB_FROM_YEAR:
        fig = f"figures/pub_{key}.jpg"
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
    return [f'{i}  <div class="pub">'] + body_lines + [f'{i}  </div>']


def _year_group_html(year: int, entries: list, indent: str = "    ") -> list[str]:
    i = indent
    parts = [f'{i}<div class="year-group">',
             f'{i}  <div class="year-label">{year}</div>']
    for key, p in entries:
        parts += _pub_html(key, year, p, i)
    parts.append(f'{i}</div>')
    return parts


def render_publications_html(publications: list[dict],
                             keys: dict[int, str]) -> str:
    groups: dict[int, list] = defaultdict(list)
    for i, p in enumerate(publications):
        groups[p["year"]].append((keys.get(i), p))

    recent_years = sorted((y for y in groups if y >= HIDE_BEFORE_YEAR), reverse=True)
    old_years    = sorted((y for y in groups if y <  HIDE_BEFORE_YEAR), reverse=True)

    parts: list[str] = []
    for year in recent_years:
        parts += _year_group_html(year, groups[year])

    if old_years:
        old_count = sum(len(groups[y]) for y in old_years)
        parts.append('    <details class="older-pubs">')
        parts.append('      <summary class="older-pubs-toggle">'
                     f'Earlier publications ({old_count})</summary>')
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
        + "\n" + pub_html + "    "
        + content[end:]
    )
    html_path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Overrides reference guide
# ---------------------------------------------------------------------------
def write_overrides_readme(pubs: list[dict], keys: dict[int, str],
                           out_path: Path) -> None:
    lines = [
        "# Figure overrides",
        "",
        "Drop a replacement image here named **`pub_<slug>.<ext>`** to override the",
        "auto-fetched thumbnail for that paper.  Any common image format works",
        "(`.jpg`, `.jpeg`, `.png`, `.webp`, …).  The image will be automatically",
        "cropped/resized to the standard thumbnail size.",
        "",
        "The workflow picks it up on the next run — no `--force` needed, overrides always win.",
        "",
        "**Naming:** each paper's slug is minted once and recorded in `figures/index.json`.",
        "It is derived from the title but never recomputed, so reordering the CV, inserting",
        "a paper, or editing a title will not rename anything. Copy the slug from the table",
        "below rather than guessing it.",
        "",
        "## Paper index",
        "",
        "| File | Year | Venue | Title |",
        "| ---- | ---- | ----- | ----- |",
    ]
    ordered = sorted(keys, key=lambda i: (pubs[i]["year"], pubs[i]["title"]))
    for i in ordered:
        p = pubs[i]
        lines.append(
            f"| `pub_{keys[i]}.jpg` | {p['year']} | "
            f"{p['venue'].replace('|', chr(92) + '|')} | "
            f"{p['title'].replace('|', chr(92) + '|')} |"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(pubs: list[dict], keys: dict[int, str], warnings: list[str],
           verbose: bool) -> None:
    by_year: dict[int, int] = defaultdict(int)
    for p in pubs:
        by_year[p["year"]] += 1

    print(f"  Parsed {len(pubs)} publications "
          f"({sum(1 for p in pubs if p['url'] != '#')} with URLs)")
    print("  By year: " + ", ".join(f"{y}:{by_year[y]}"
                                    for y in sorted(by_year, reverse=True)))

    if verbose:
        print()
        for i, p in enumerate(pubs):
            badge = f"  [{p['badge']}]" if p["badge"] else ""
            print(f"  {p['year']}  {p['title'][:78]}{badge}")
            print(f"        authors: {p['authors'][:88]}")
            print(f"        venue:   {p['venue'][:88]}")
            print(f"        url:     {p['url'][:88]}")
            if i in keys:
                print(f"        thumb:   figures/pub_{keys[i]}.jpg")

    if warnings:
        print(f"\n  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"    ! {w}")


# ---------------------------------------------------------------------------
# One-shot migration from the old positional numbering
# ---------------------------------------------------------------------------
def migrate_numeric(pubs: list[dict], keys: dict[int, str], figures_dir: Path,
                    overrides_dir: Path, legacy: dict[str, str] | None = None,
                    apply: bool = False) -> None:
    """Rename pub_NN.* files to their stable slug names.

    By default a file's old number is reconstructed with the old positional
    formula. `legacy` overrides that for files the old parser mislabelled: it
    maps an old number (as a string) to a substring of the title the file
    actually depicts, e.g. {"10": "Compressing Observation History"}.
    """
    old_idx = thumb_index_map(pubs)

    if legacy:
        for num_s, title_hint in legacy.items():
            num = int(num_s)
            hint = normalize_title(title_hint)
            target = next((i for i in keys
                           if hint in normalize_title(pubs[i]["title"])), None)
            if target is None:
                print(f"  ! legacy map: no paper matches {title_hint!r}, ignoring")
                continue
            # Nobody else may claim this number.
            for i, v in list(old_idx.items()):
                if v == num:
                    old_idx[i] = None
            old_idx[target] = num

    exts = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"]
    moves: list[tuple[Path, Path]] = []

    for i, slug in keys.items():
        num = old_idx.get(i)
        if num is None:
            continue
        for folder in (figures_dir, overrides_dir):
            for stem in dict.fromkeys((f"pub_{num:02d}", f"pub_{num}")):
                for ext in exts:
                    src = folder / f"{stem}{ext}"
                    if src.exists():
                        moves.append((src, folder / f"pub_{slug}{ext}"))

    if not moves:
        print("  Nothing to migrate — no pub_NN.* files found.")
        return

    for src, dst in moves:
        print(f"  {'mv' if apply else 'would mv'}  {src}  ->  {dst.name}")
        if apply:
            if dst.exists():
                print(f"     ! {dst} already exists, skipping")
                continue
            src.rename(dst)

    if not apply:
        print("\n  Dry run. Re-run with --apply to perform the renames, or copy "
              "these into git mv commands to keep the history.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update publications in index.html from cv.pdf"
    )
    parser.add_argument("--cv",   default="cv.pdf",     help="Path to the CV PDF")
    parser.add_argument("--html", default="index.html", help="Path to index.html")
    parser.add_argument("--overrides-dir", default="figures/overrides",
                        help="Where to write the override README")
    parser.add_argument("--figures-dir", default="figures",
                        help="Where the thumbnails live")
    parser.add_argument("--registry", default=REGISTRY_PATH,
                        help="Slug registry JSON (keep this in git)")
    parser.add_argument("--check", action="store_true",
                        help="Parse and report only; write nothing")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print every parsed record")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if the parse produced warnings")
    parser.add_argument("--min-pubs", type=int, default=1,
                        help="Fail if fewer than this many publications are parsed")
    parser.add_argument("--migrate", action="store_true",
                        help="One-shot: rename legacy pub_NN.* files to slug names")
    parser.add_argument("--apply", action="store_true",
                        help="With --migrate, actually perform the renames")
    parser.add_argument("--legacy", default=None,
                        help='With --migrate: JSON file mapping an old number to '
                             'a substring of the title that file really shows, '
                             'e.g. {"10": "Compressing Observation History"}')
    args = parser.parse_args()

    cv_path   = Path(args.cv)
    html_path = Path(args.html)

    if not cv_path.exists():
        sys.exit(f"CV not found: {cv_path}")
    if not (args.check or args.migrate) and not html_path.exists():
        sys.exit(f"HTML file not found: {html_path}")

    print(f"Parsing {cv_path} …")
    pubs, warnings = parse_cv(cv_path)

    if args.migrate:
        keys = assign_keys(pubs, args.registry, write=args.apply,
                           warnings=warnings)
        legacy = (json.loads(Path(args.legacy).read_text(encoding="utf-8"))
                  if args.legacy else None)
        print(f"  {len(pubs)} publications, {len(keys)} with thumbnails\n")
        migrate_numeric(pubs, keys, Path(args.figures_dir),
                        Path(args.overrides_dir), legacy=legacy,
                        apply=args.apply)
        return

    keys = assign_keys(pubs, args.registry, write=not args.check,
                       warnings=warnings)
    report(pubs, keys, warnings, verbose=args.verbose or args.check)

    if len(pubs) < args.min_pubs:
        sys.exit(f"\nOnly {len(pubs)} publications parsed, expected at least "
                 f"{args.min_pubs}. Refusing to update. Run --check --verbose "
                 f"to inspect.")

    if args.check:
        print("\n--check: nothing written.")
    else:
        print(f"\nUpdating {html_path} …")
        inject_into_html(html_path, render_publications_html(pubs, keys))
        write_overrides_readme(pubs, keys, Path(args.overrides_dir) / "README.md")
        print(f"Wrote {args.registry}")
        print("Done.")

    if warnings and args.strict:
        sys.exit(f"\n--strict: {len(warnings)} warning(s), exiting non-zero.")


if __name__ == "__main__":
    main()
