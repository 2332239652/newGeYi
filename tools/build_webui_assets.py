"""Build pixel-only GeYi WebUI assets and sync Base64 data URIs in webui/index.html.
Run using the bundled Python with Pillow. Original files remain unchanged.
"""
from pathlib import Path
from collections import deque
import base64
import json
import re
from PIL import Image
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PORTRAIT_SOURCE = Path(r"<素材目录>\歌伊.png")
AVATAR_SOURCE = Path(r"<素材目录>\头像源图.png")
OUT = ROOT / "webui" / "assets"
HTML_PATH = ROOT / "webui" / "index.html"


def outer_white_transparent(image):
    """Remove only near-white pixels connected to canvas edges, never global white."""
    a = np.array(image.convert("RGBA"), dtype=np.uint8)
    rgb = a[:, :, :3]
    white = (rgb.min(axis=2) >= 248) & (rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int) <= 7)
    h, w = white.shape
    seen = np.zeros((h, w), dtype=bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if white[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if white[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= yy < h and 0 <= xx < w and white[yy, xx] and not seen[yy, xx]:
                seen[yy, xx] = True
                q.append((yy, xx))
    a[seen, 3] = 0
    return Image.fromarray(a), int(seen.sum())


def sync_html():
    if not HTML_PATH.is_file():
        return
    html = HTML_PATH.read_text(encoding="utf-8")
    for dom_id, filename in (("geyi-avatar", "geyi-avatar.webp"), ("geyi-portrait", "geyi-portrait.webp")):
        b64 = base64.b64encode((OUT / filename).read_bytes()).decode("ascii")
        pattern = r'(<img id="' + dom_id + r'"[^>]*\bsrc=")(?:data:image/webp;base64,[A-Za-z0-9+/=]+|__GEYI_[A-Z_]+__)(")'
        html, count = re.subn(pattern, r"\1data:image/webp;base64," + b64 + r"\2", html, count=1)
        assert count == 1, f"Failed to update {dom_id} in {HTML_PATH}"
    HTML_PATH.write_text(html, encoding="utf-8")


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "metadata": "Pixel buffers only; no source EXIF/comments/XMP/ICC read or copied",
        "assets": [],
    }
    recipes = [
        (
            "geyi-portrait.webp",
            PORTRAIT_SOURCE,
            (0, 0, 2942, 4035),
            (630, 864),
            88,
            "outer_white_flood",
        ),
        (
            "geyi-avatar.webp",
            AVATAR_SOURCE,
            (100, 140, 1948, 1988),
            (192, 192),
            92,
            "preserve_rgba",
        ),
    ]
    for name, src_path, box, size, quality, alpha_mode in recipes:
        with Image.open(src_path) as source:
            fmt, original_size = source.format, source.size
            pixels = source.convert("RGBA")
        cropped = pixels.crop(box).resize(size, Image.Resampling.LANCZOS)
        if alpha_mode == "outer_white_flood":
            clean, removed = outer_white_transparent(cropped)
        else:
            clean, removed = Image.frombytes("RGBA", size, cropped.tobytes()), 0
        target = OUT / name
        clean.save(target, "WEBP", quality=quality, method=6, exact=True)
        with Image.open(target) as check:
            assert check.format == "WEBP" and check.size == size
            assert not any(k in check.info for k in ("exif", "xmp", "icc_profile", "comment"))
        report["assets"].append({
            "path": str(target.relative_to(ROOT)),
            "source_name": src_path.name,
            "source_format": fmt,
            "source_size": list(original_size),
            "source_bytes": src_path.stat().st_size,
            "crop_bbox": list(box),
            "size": list(size),
            "quality": quality,
            "method": 6,
            "bytes": target.stat().st_size,
            "alpha_mode": alpha_mode,
            "transparent_pixels_removed": removed,
        })
    sync_html()
    report["html_synced"] = str(HTML_PATH.relative_to(ROOT))
    report["html_bytes"] = HTML_PATH.stat().st_size
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build()
