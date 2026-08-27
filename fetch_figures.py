#!/usr/bin/env python3
"""
fetch_figures.py
================
For each publication from THUMB_FROM_YEAR onwards, fetch a representative
image and save it as figures/pub_NN.jpg (280×180 px).

Image extraction strategy (in order):
  1. DuckDuckGo image search on the paper title → first usable result
  2. arxiv URL  → download PDF → extract largest embedded image (pypdf)
  3. Any URL    → fetch HTML  → read og:image / twitter:image
  4. Any URL    → fetch HTML  → find linked PDF → extract figure
  5. Direct PDF → extract largest embedded image
  6. pdf2image  → render page 1, crop middle content strip
  7. Placeholder (styled with venue + year)

Usage:
    python fetch_figures.py --cv cv.pdf --out figures [--force]
"""

import argparse, io, re, sys, time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlparse, urljoin
from urllib.error import URLError, HTTPError

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required:  pip install Pillow")
try:
    import pypdf
except ImportError:
    sys.exit("pypdf required:  pip install pypdf")

try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

sys.path.insert(0, str(Path(__file__).parent))
from update_pubs import extract_publications, assign_keys, REGISTRY_PATH

# ── Config ────────────────────────────────────────────────────────────────────
THUMB_FROM_YEAR = 2016          # include thumbnails for this year and later
THUMB_W, THUMB_H = 280, 180     # final thumbnail dimensions
MIN_FIG_PX      = 120           # minimum side length to accept an embedded image
MIN_IMG_W       = 100           # minimum width for image-search results
MIN_IMG_H       = 80            # minimum height for image-search results
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch(url, timeout=25):
    """Return (bytes, content-type) or (None, None) on failure."""
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/pdf,image/*,*/*;q=0.8",
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except Exception as e:
        print(f"      fetch error: {e}")
        return None, None


# ── DuckDuckGo image search ───────────────────────────────────────────────────
def search_image_for_title(title):
    """Search DuckDuckGo Images for the paper title; return first usable PIL image."""
    if not HAS_DDGS:
        return None
    query = f"{title} research paper"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=8))
        for r in results:
            img_url = r.get("image", "")
            if not img_url:
                continue
            print(f"      image search → {img_url[:90]}")
            data, ct = fetch(img_url)
            if not data:
                continue
            try:
                pil = Image.open(io.BytesIO(data))
                if pil.width >= MIN_IMG_W and pil.height >= MIN_IMG_H:
                    return pil
            except Exception:
                pass
    except Exception as e:
        print(f"      ddgs error: {e}")
    return None


# ── arxiv helpers ─────────────────────────────────────────────────────────────
def arxiv_pdf_url(url):
    m = re.search(r'arxiv\.org/(?:abs|pdf)/([^\s\?#/]+(?:/\d+)?)', url)
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else None


# ── HTML helpers ──────────────────────────────────────────────────────────────
def og_image(html_bytes, base_url):
    html = html_bytes.decode("utf-8", errors="ignore")
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            img_url = m.group(1).strip()
            return img_url if img_url.startswith("http") else urljoin(base_url, img_url)
    return None


def pdf_link(html_bytes, base_url):
    html = html_bytes.decode("utf-8", errors="ignore")
    for pat in [
        r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
        r'href=["\']([^"\']+/pdf/[^"\']+)["\']',
        r'href=["\']([^"\']+[?&]format=pdf[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            u = m.group(1).strip()
            return u if u.startswith("http") else urljoin(base_url, u)
    return None


# ── PDF figure extraction ─────────────────────────────────────────────────────
def figures_from_pdf(pdf_bytes):
    """Extract the largest embedded image from pages 1-4 using pypdf."""
    best, best_area = None, 0
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        for page_num in range(min(4, len(reader.pages))):
            try:
                for img_file in reader.pages[page_num].images:
                    try:
                        pil = img_file.image
                        if pil.width >= MIN_FIG_PX and pil.height >= MIN_FIG_PX:
                            area = pil.width * pil.height
                            if area > best_area:
                                best_area = area
                                best = pil.copy()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        print(f"      pypdf error: {e}")
    return best


def render_pdf_page(pdf_bytes):
    """Fallback: render page 1 with pdf2image, return middle content strip."""
    if not HAS_PDF2IMAGE:
        return None
    try:
        pages = convert_from_bytes(pdf_bytes, dpi=130, first_page=1, last_page=1)
        if pages:
            pg = pages[0]
            w, h = pg.size
            return pg.crop((0, int(h * 0.18), w, int(h * 0.85)))
    except Exception as e:
        print(f"      pdf2image error: {e}")
    return None


# ── Main per-paper logic ──────────────────────────────────────────────────────
def get_image_for(title, url):
    """Return a PIL Image or None, trying image search first then URL-based fallbacks."""

    # 1 ── DuckDuckGo image search on paper title ─────────────────────────────
    img = search_image_for_title(title)
    if img:
        return img

    # 2 ── arxiv: go straight to PDF ──────────────────────────────────────────
    if "arxiv.org" in url:
        pdf_url = arxiv_pdf_url(url)
        if pdf_url:
            print(f"      arxiv PDF → {pdf_url}")
            data, ct = fetch(pdf_url)
            if data:
                img = figures_from_pdf(data)
                if img:
                    return img
                img = render_pdf_page(data)
                if img:
                    return img

    # 3 ── direct PDF URL ─────────────────────────────────────────────────────
    if url.lower().endswith(".pdf") or re.search(r'/pdf/', url, re.I):
        data, ct = fetch(url)
        if data and ("pdf" in (ct or "") or url.lower().endswith(".pdf")):
            img = figures_from_pdf(data)
            if img:
                return img
            img = render_pdf_page(data)
            if img:
                return img

    # 4 ── HTML page ───────────────────────────────────────────────────────────
    html_data, ct = fetch(url)
    if not html_data:
        return None

    # 4a: og:image / twitter:image
    og = og_image(html_data, url)
    if og:
        print(f"      og:image → {og}")
        img_data, _ = fetch(og)
        if img_data:
            try:
                pil = Image.open(io.BytesIO(img_data))
                if pil.width >= 80 and pil.height >= 80:
                    return pil
            except Exception:
                pass

    # 4b: find linked PDF → extract figure
    pdf_href = pdf_link(html_data, url)
    if pdf_href:
        print(f"      linked PDF → {pdf_href}")
        pdf_data, _ = fetch(pdf_href)
        if pdf_data:
            img = figures_from_pdf(pdf_data)
            if img:
                return img
            img = render_pdf_page(pdf_data)
            if img:
                return img

    return None


# ── Resize / crop to thumbnail ────────────────────────────────────────────────
def to_thumb(img):
    img = img.convert("RGB")
    src_ratio = img.width / img.height
    dst_ratio = THUMB_W / THUMB_H
    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        left  = (img.width - new_w) // 2
        img   = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / dst_ratio)
        top   = img.height // 6
        top   = min(top, img.height - new_h)
        img   = img.crop((0, top, img.width, top + new_h))
    return img.resize((THUMB_W, THUMB_H), Image.LANCZOS)


# ── Placeholder ───────────────────────────────────────────────────────────────
def make_placeholder(pub):
    words  = re.findall(r'\b[A-Z][A-Z0-9]{1,}\b', pub["venue"])
    abbrev = words[0] if words else pub["venue"][:5].upper()
    bg, accent = (240, 242, 245), (29, 78, 216)
    img  = Image.new("RGB", (THUMB_W, THUMB_H), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 5, THUMB_H], fill=accent)
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            fb = ImageFont.truetype(path, 30)
            fs = ImageFont.truetype(path.replace("Bold", "Regular").replace("-Bold",""), 16)
            break
        except Exception:
            fb = fs = ImageFont.load_default()
    cx, cy = THUMB_W // 2, THUMB_H // 2
    draw.text((cx, cy - 16), abbrev,         fill=accent,         font=fb, anchor="mm")
    draw.text((cx, cy + 20), str(pub["year"]), fill=(100, 116, 139), font=fs, anchor="mm")
    return img


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Fetch paper thumbnails")
    parser.add_argument("--cv",        default="cv.pdf")
    parser.add_argument("--out",       default="figures")
    parser.add_argument("--overrides", default="figures/overrides",
                        help="Folder of manually-supplied images (pub_NN.{jpg,png,…})")
    parser.add_argument("--force",     action="store_true", help="Re-fetch even if file exists")
    parser.add_argument("--registry",  default=REGISTRY_PATH,
                        help="Slug registry JSON written by update_pubs.py")
    parser.add_argument("--prune",     action="store_true",
                        help="Delete pub_*.jpg files no longer referenced by any paper")
    args = parser.parse_args()

    cv_path       = Path(args.cv)
    out_dir       = Path(args.out)
    overrides_dir = Path(args.overrides)
    out_dir.mkdir(exist_ok=True)

    pubs = extract_publications(cv_path)
    n    = len(pubs)

    # ── Stable per-paper slugs ───────────────────────────────────────────────
    # Filenames are figures/pub_<slug>.jpg, where the slug is minted once and
    # recorded in the registry. Position in the CV is irrelevant, so reordering
    # or inserting papers never reshuffles thumbnails or orphans an override.
    # update_pubs.py owns the registry; we read it so the HTML and the figures
    # can never disagree.
    keys = assign_keys(pubs, args.registry, write=True)

    # Image-format extensions to check in the overrides folder
    OVERRIDE_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"]

    def find_override(key: str) -> Path | None:
        for ext in OVERRIDE_EXTS:
            p = overrides_dir / f"pub_{key}{ext}"
            if p.exists():
                return p
        return None

    real = placeholder = skipped = overridden = 0

    for i in range(n):
        pub  = pubs[i]
        url  = pub["url"]
        year = pub["year"]

        if year < THUMB_FROM_YEAR or i not in keys:
            continue

        idx = keys[i]
        dst = out_dir / f"pub_{idx}.jpg"

        # ── manual override wins unconditionally ──────────────────────────────
        override_path = find_override(idx)
        if override_path:
            try:
                img = Image.open(override_path)
                to_thumb(img).save(dst, "JPEG", quality=85, optimize=True)
                print(f"[{idx}] override ({override_path.suffix})  {year}  {pub['title'][:50]}")
                overridden += 1
            except Exception as e:
                print(f"[{idx}] override error: {e} — skipping")
            continue

        # ── skip if already fetched (unless --force) ──────────────────────────
        if dst.exists() and not args.force:
            print(f"[{idx}] skip  {year}  {pub['title'][:55]}")
            skipped += 1
            continue

        print(f"[{idx}] fetch {year}  {pub['title'][:55]}")
        img = get_image_for(pub["title"], url)

        if img:
            to_thumb(img).save(dst, "JPEG", quality=85, optimize=True)
            print(f"      ✓ {dst.name}")
            real += 1
        else:
            print(f"      → placeholder")
            make_placeholder(pub).save(dst, "JPEG", quality=85)
            placeholder += 1

        time.sleep(1.5)   # polite pause between papers

    if args.prune:
        wanted = {f"pub_{k}.jpg" for k in keys.values()}
        for stale in sorted(out_dir.glob("pub_*.jpg")):
            if stale.name not in wanted:
                print(f"      prune {stale.name}")
                stale.unlink()

    print(f"\nDone: {overridden} override  |  {real} fetched  |  {placeholder} placeholder  |  {skipped} skipped")
    print(f"      ({len(keys)} papers with thumbnails; slugs recorded in {args.registry})")


if __name__ == "__main__":
    main()
