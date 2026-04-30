#!/usr/bin/env python3
"""
fetch_figures.py
================
Downloads each paper PDF linked in cv.pdf, extracts the first prominent
figure, and saves it as figures/pub_NN.jpg (280×180 px).

Run this once after cloning, and again whenever you add new publications.
Already-fetched figures are skipped (delete a file to re-fetch it).

Requirements:
    pip install pdfplumber pdf2image requests Pillow

System packages required by pdf2image:
    Ubuntu/Debian:  sudo apt-get install poppler-utils
    macOS:          brew install poppler
"""

import io
import re
import sys
import json
import time
import shutil
import hashlib
import argparse
import tempfile
import subprocess
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    import pdfplumber
except ImportError:
    sys.exit("Missing: pip install pdfplumber")
try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Missing: pip install Pillow")

PDF2IMAGE_OK = False
try:
    from pdf2image import convert_from_bytes
    PDF2IMAGE_OK = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FIG_W, FIG_H = 280, 180          # output image size (px)
MAX_PDF_MB   = 8                  # maximum PDF size to download (MB)
TIMEOUT      = 30                 # HTTP timeout (s)
PAUSE        = 1.2                # polite pause between downloads (s)
MIN_IMG_PX   = 120                # minimum side length to consider an image a "figure"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; academic-website-builder/1.0; "
        "+https://github.com/gmonaci/gmonaci.github.io)"
    )
}

# Accent colours for placeholders (cycling)
ACCENTS = [
    (37, 99, 235), (5, 150, 105), (124, 58, 237),
    (220, 38, 38), (217, 119, 6),
]

# ---------------------------------------------------------------------------
# Encoding fixes (same as update_pubs.py)
# ---------------------------------------------------------------------------
ENCODING_FIXES = [
    ("Su¨sstrunk","Süsstrunk"),("Bu¨lent","Bülent"),
    ("´e","é"),("´E","É"),("Herv´e","Hervé"),("D´ejean","Déjean"),
    ("St´ephane","Stéphane"),("Fr´ed´eric","Frédéric"),("R´emi","Rémi"),
    ("O`scar","Óscar"),
]

BADGES = ["Oral","Spotlight","Highlight","Best Paper Finalist","Best Student Paper"]
year_re = re.compile(r"\b(19|20)\d{2}$")


def fix_encoding(s):
    for a, b in ENCODING_FIXES:
        s = s.replace(a, b)
    return s


# ---------------------------------------------------------------------------
# Extract publications + URLs from cv.pdf
# ---------------------------------------------------------------------------
def extract_pub_urls(pdf_path: Path) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        urls = []
        for page_num in [1, 2, 3]:
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
        full = "\n".join(p.extract_text() or "" for p in pdf.pages)
    text = fix_encoding(full)
    m = re.search(
        r"PUBLICATIONS\n(.*?)(?:PATENTS AND PATENT APPLICATIONS|$)", text, re.DOTALL
    )
    if not m:
        raise ValueError("No PUBLICATIONS section found")
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    pubs, cur = [], []
    for line in lines:
        cur.append(line)
        if year_re.search(line):
            if len(cur) >= 2:
                title = cur[0]
                badge = None
                for b in BADGES:
                    pat = re.compile(rf"\s*\({re.escape(b)}\)")
                    if pat.search(title):
                        title = pat.sub("", title).strip()
                        badge = b
                        break
                ym = re.search(r"\b((19|20)\d{2})$", cur[-1])
                pubs.append(dict(
                    title=title,
                    venue=fix_encoding(cur[-1]),
                    year=int(ym.group(1)) if ym else 0,
                    badge=badge,
                ))
            cur = []
    return pubs


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def is_direct_pdf_url(url: str) -> bool:
    """Heuristic: URL likely points directly to a PDF file."""
    u = url.lower()
    return (
        u.endswith(".pdf")
        or "/pdf/" in u
        or "pdf?id=" in u
        or "/download/pdf/" in u
        or "/bitstreams/" in u
    )


def resolve_to_pdf_url(url: str) -> str | None:
    """
    Given a paper page URL (IEEE, Springer, ScienceDirect, NaverLabs…),
    try Semantic Scholar to find an open-access PDF.
    """
    # Semantic Scholar search by URL as identifier
    ss_url = (
        "https://api.semanticscholar.org/graph/v1/paper/search"
        "?query=" + requests.utils.quote(url, safe="")
        + "&fields=title,openAccessPdf"
        + "&limit=1"
    )
    try:
        resp = requests.get(ss_url, headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()
        papers = data.get("data", [])
        if papers:
            oa = papers[0].get("openAccessPdf")
            if oa and oa.get("url"):
                return oa["url"]
    except Exception:
        pass
    return None


def download_pdf_bytes(url: str, max_mb: float = MAX_PDF_MB) -> bytes | None:
    """Download a PDF, aborting if it exceeds max_mb."""
    max_bytes = int(max_mb * 1024 * 1024)
    try:
        with requests.get(
            url, headers=HEADERS, timeout=TIMEOUT, stream=True
        ) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" not in ctype and not url.lower().endswith(".pdf"):
                # Follow if it redirected to HTML (abstract page instead of PDF)
                return None
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    return None  # too big
            return b"".join(chunks)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Figure extraction
# ---------------------------------------------------------------------------
def extract_first_figure_from_pdf(pdf_bytes: bytes) -> Image.Image | None:
    """
    Try two strategies:
    1. Extract embedded images from pages 1-4 via pypdf, pick the largest.
    2. Render page 1 as raster via pdf2image as fallback.
    Returns a PIL Image or None.
    """
    # Strategy 1: embedded images via pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        best: tuple[int, Image.Image] = (0, None)
        for page_num in range(min(4, len(reader.pages))):
            page = reader.pages[page_num]
            for name, img_obj in page.images:
                try:
                    data = img_obj.data
                    pil = Image.open(io.BytesIO(data)).convert("RGB")
                    area = pil.width * pil.height
                    if pil.width >= MIN_IMG_PX and pil.height >= MIN_IMG_PX and area > best[0]:
                        best = (area, pil)
                except Exception:
                    continue
        if best[1] is not None:
            return best[1]
    except Exception:
        pass

    # Strategy 2: render page 1 via pdf2image
    if PDF2IMAGE_OK:
        try:
            pages = convert_from_bytes(pdf_bytes, dpi=120, first_page=1, last_page=1)
            if pages:
                return pages[0]
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Placeholder generator
# ---------------------------------------------------------------------------
def venue_abbrev(venue: str) -> str:
    abbrevs = {
        "CVPR": "CVPR", "ICCV": "ICCV", "ECCV": "ECCV",
        "NeurIPS": "NeurIPS", "ICLR": "ICLR", "ICML": "ICML",
        "TMLR": "TMLR", "RSS": "RSS", "IROS": "IROS", "ICRA": "ICRA",
        "3DV": "3DV", "RO-MAN": "RO-MAN", "ICIP": "ICIP",
        "ICASSP": "ICASSP", "EUSIPCO": "EUSIPCO",
        "ACM": "ACM MM", "IEEE Transactions": "IEEE Trans.",
        "Signal Processing": "Signal Proc.", "Computer Vision and Pattern Recognition": "CVPR",
        "Computer Vision and Image Understanding": "CVIU",
    }
    for key, abbr in abbrevs.items():
        if key.lower() in venue.lower():
            return abbr
    m = re.search(r"\(([A-Z][A-Z0-9\-]{1,8})\)", venue)
    return m.group(1) if m else venue.split(",")[0][:12]


def make_placeholder(pub: dict, accent: tuple) -> Image.Image:
    img = Image.new("RGB", (FIG_W, FIG_H), (245, 246, 248))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 5, FIG_H], fill=accent)
    for y in range(0, FIG_H, 30):
        draw.line([(6, y), (FIG_W, y)], fill=(230, 231, 233))
    for x in range(30, FIG_W, 40):
        draw.line([(x, 0), (x, FIG_H)], fill=(230, 231, 233))
    abbr = venue_abbrev(pub["venue"])
    year = str(pub["year"])
    try:
        f_big = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26
        )
        f_sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16
        )
    except Exception:
        f_big = f_sm = ImageFont.load_default()
    for text, font, dy in [(abbr, f_big, -28), (year, f_sm, 10)]:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((FIG_W - tw) // 2 + 3, FIG_H // 2 + dy), text,
                  fill=(60, 70, 90) if dy < 0 else (130, 140, 155), font=font)
    draw.rectangle([0, 0, FIG_W - 1, FIG_H - 1], outline=(210, 213, 218))
    return img


# ---------------------------------------------------------------------------
# Crop / resize to standard size
# ---------------------------------------------------------------------------
def resize_and_crop(img: Image.Image) -> Image.Image:
    """Centre-crop to 7:4.5 aspect ratio, then resize to FIG_W × FIG_H."""
    target_ratio = FIG_W / FIG_H
    src_ratio = img.width / img.height
    if src_ratio > target_ratio:
        # too wide: crop sides
        new_w = int(img.height * target_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        # too tall: crop top/bottom
        new_h = int(img.width / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((FIG_W, FIG_H), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch first figure for each publication in cv.pdf"
    )
    parser.add_argument("--cv",      default="cv.pdf",    help="Path to CV PDF")
    parser.add_argument("--out",     default="figures",   help="Output directory")
    parser.add_argument("--force",   action="store_true", help="Re-fetch existing figures")
    args = parser.parse_args()

    cv_path  = Path(args.cv)
    out_dir  = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    if not cv_path.exists():
        sys.exit(f"CV not found: {cv_path}")

    print(f"Parsing {cv_path}…")
    pubs = extract_publications(cv_path)
    urls = extract_pub_urls(cv_path)

    if len(pubs) != len(urls):
        print(f"WARNING: {len(pubs)} pubs but {len(urls)} URLs; will process up to min of both")

    n = min(len(pubs), len(urls))
    success = 0

    for i in range(n):
        pub = pubs[i]
        url = urls[i]
        out_path = out_dir / f"pub_{i+1:02d}.jpg"

        if out_path.exists() and not args.force:
            print(f"  [{i+1:2d}/{n}] skip (exists): {pub['title'][:50]}")
            continue

        print(f"  [{i+1:2d}/{n}] {pub['title'][:55]}")
        print(f"        URL: {url[:70]}")

        # Resolve to PDF if needed
        pdf_url = url if is_direct_pdf_url(url) else resolve_to_pdf_url(url)

        figure = None
        if pdf_url:
            pdf_bytes = download_pdf_bytes(pdf_url)
            if pdf_bytes:
                print(f"        Downloaded {len(pdf_bytes)//1024} KB")
                figure = extract_first_figure_from_pdf(pdf_bytes)
                if figure:
                    print(f"        ✓ extracted figure {figure.width}×{figure.height}")
                else:
                    print(f"        ✗ no figure found in PDF")
            else:
                print(f"        ✗ download failed")
        else:
            print(f"        ✗ could not resolve to PDF URL")

        if figure is None:
            figure = make_placeholder(pub, ACCENTS[i % len(ACCENTS)])
            print(f"        → using placeholder")
        else:
            figure = figure.convert("RGB")
            success += 1

        resize_and_crop(figure).save(out_path, "JPEG", quality=88)
        time.sleep(PAUSE)

    print(f"\nDone: {success}/{n} real figures fetched, rest are placeholders.")
    print(f"Figures saved to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
