#!/usr/bin/env python3
"""
Generate payloads.json for a PS5 Payload Manager custom repository.

Matches the richer field set used by itsPLK/ps5-payloads-mirror
(payloads_v2.json): name, filename, url, source, source_direct,
description, last_update, version, category, checksum — while NOT
mirroring binaries. `url` and `source_direct` both point straight at
the upstream release asset; `source` is the upstream repo's releases
page. If you later want to actually mirror binaries into a GitHub
Release of your own, `url` would need to change to point there instead
— that's a separate, bigger change (download + re-upload as a release
asset), not something this script does.

links.txt format
-----------------
One entry per line. Blank lines and lines starting with '#' are ignored.

1. GitHub repo shorthand — resolved against that repo's LATEST release,
   one payload entry per matching release asset:

       github:owner/repo
       github:owner/repo | category
       github:owner/repo | category | description | version

   Key=value overrides (any order, anywhere in the line):
     name=...            override payload name
     filename=...         override filename
     asset_pattern=...    only use assets whose filename contains this
                           substring (case-insensitive) — use when a
                           release has multiple assets and you only
                           want one, e.g. asset_pattern=ps5
     extract_file=...     informational only: the file inside a zip/tar
                           asset that is the actual payload to run.
                           Recorded on the entry if the asset itself
                           isn't a raw payload file.
     icon=...             override the payload icon — a direct image URL,
                           a GitHub blob URL, @owner, or `none` to
                           disable the icon (details under Icons below)

   `version` defaults to the release tag; `description` defaults to the
   release name; `last_update` is always taken from the release's
   publish date (UTC, YYYY-MM-DD) and cannot be overridden.

2. A direct HTTP(S) URL:

       https://example.com/payloads/ftpsrv_v0.19.elf
       https://example.com/payloads/ftpsrv_v0.19.elf | category | description | version

   `source` and `source_direct` are set equal to `url` itself (there's
   no separate upstream to point to), and `last_update` is omitted
   since no release metadata is available for a bare URL.

Only .elf and .bin files are included in payloads.json. For
`github:owner/repo` entries, non-.elf/.bin release assets are filtered
out before anything else (including asset_pattern matching); if a
release has none, that repo is skipped with a warning. For direct URL
entries, a URL whose filename doesn't end in .elf or .bin is skipped
with a warning rather than included.

Icons
-----
PS5 Payload Manager stores icons in icons.json as data URIs, keyed by
icon_key (SHA-256 of the stored image bytes), and PS5 WebKit rejects any
data URI at or above 12,288 characters. Everything below exists to put a
real image into that budget instead of a generated letter tile.

For each entry, the icon is resolved in this order:

  1. `icon=<value>` override in links.txt, if present. Accepted forms:
       icon=https://example.com/logo.png   any direct image URL
       icon=https://github.com/o/r/blob/main/docs/logo.png
                                           GitHub blob URL (converted to raw)
       icon=@owner or icon=https://github.com/owner
                                           that account's avatar
       icon=none                           no icon at all (no icon_key)
  2. Otherwise, for entries whose source is a GitHub repo: the repo's
     default branch is read, its file tree is listed, and every image in
     the repo is scored by filename (icon0 > launcher/app icon > icon >
     logo > avatar > emblem/brand/mark > banner), path depth and file
     type. Screenshots, QR codes, flags, donation links, social previews,
     splash/background art and build fixtures are excluded. The best
     scoring candidate is used; images below 48px (favicons, thumbnails)
     are skipped. A wide banner beats the owner avatar, because it is the
     project's own artwork.
  3. Images referenced from the repo README are scored the same way and
     tried next, so a logo that only lives in docs/ and is displayed via
     <img src="docs/logo.png"> is still found.
  4. The repo owner's GitHub avatar (https://github.com/<owner>.png?size=128),
     which is already small enough to pass the limit uncompressed.
  5. A generated gradient SVG with the payload's initials, so every entry
     always has something to show.

Anything larger than the limit is downscaled and re-encoded with Pillow
(128px max, PNG, then palette-quantized PNG or JPEG if that is still too
big). Images that are not square are shaped for the square tile: a
mildly non-square image is center-cropped, and a wide banner is drawn
whole on a blurred, darkened copy of itself so nothing is cut off.
Pillow is optional: without it, oversized images fall back to the
generated SVG and a warning is printed, so `pip install -r
requirements.txt` (done in CI) is what makes real icons work for large
PNGs.

icons.json ends up as {"icons": {<icon_key>: "data:image/<type>;base64,..."}},
referenced from payloads.json via top-level "icons_url" and per-entry
"icon_key". Identical images dedupe to one key automatically.

REPO_NAME overrides the top-level catalog name via env var.
GITHUB_TOKEN, if set, authenticates GitHub API calls.
"""

import base64
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:  # optional: downscales oversized icons below the PS5 WebKit limit
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    HAVE_PIL = True
except ImportError:  # pragma: no cover - CI installs Pillow
    Image = ImageEnhance = ImageFilter = ImageOps = None
    HAVE_PIL = False

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"
ICONS_FILE = "icons.json"
REPO_NAME = os.environ.get("REPO_NAME", "Custom Payload Repository")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DEFAULT_CATEGORY = "Uncategorized"
USER_AGENT = "payload-catalog-generator/5.0"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
GITHUB_API = "https://api.github.com"

OVERRIDE_KEYS = (
    "name",
    "filename",
    "category",
    "description",
    "version",
    "asset_pattern",
    "extract_file",
    "icon",
)

PAYLOAD_EXTENSIONS = (".elf", ".bin")

MIME_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"<?xml", "image/svg+xml"),
    (b"<svg", "image/svg+xml"),
)

# PS5 WebKit hard limit on data URI length in icons.json
ICON_MAX_CHARS = 12288
# Aim slightly under the hard limit when re-encoding
ICON_TARGET_CHARS = 12000
ICON_MAX_DIM = 128
ICON_MIN_DIM = 48  # anything smaller is a favicon/thumbnail, not a tile icon
GITHUB_AVATAR_RES = 128

ICON_EXTENSIONS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")

# Paths that are never a project icon even when their filename matches.
ICON_SKIP_MARKERS = (
    "screenshot",
    "social-preview",
    "coming-soon",
    "splash",
    "background",
    "/bg.",
    "/qr/",
    "-qr.",
    "/flags/",
    "donate",
    "sponsor",
    "shields.io",
    "node_modules",
    "/test/",
    "/tests/",
    "/fixtures/",
    "/example",
    "/sample",
    ".github/",
    "license",
    "changelog",
    "/vendor/",
)

ICON_EXT_BONUS = {"png": 0, "svg": 4, "jpg": 12, "jpeg": 12, "webp": 25, "gif": 30}

# A candidate must score at or below this to be picked automatically: it has
# to look like a deliberate icon/logo/banner, not just any image in the repo.
ICON_MAX_AUTO_SCORE = 160

# Gradient palette for fallback SVG icons
ICON_PALETTE = [
    ("#3b82f6", "#1d4ed8"),  # blue
    ("#10b981", "#047857"),  # green
    ("#8b5cf6", "#6d28d9"),  # purple
    ("#ef4444", "#b91c1c"),  # red
    ("#f59e0b", "#b45309"),  # amber
    ("#ec4899", "#be185d"),  # pink
]

ICON_SVG_TEMPLATE = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='128' height='128'>"
    "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
    "<stop offset='0' stop-color='{c1}'/><stop offset='1' stop-color='{c2}'/>"
    "</linearGradient></defs>"
    "<rect width='128' height='128' rx='24' fill='url(#g)'/>"
    "<text x='64' y='82' font-family='Arial,sans-serif' font-size='44' "
    "font-weight='bold' fill='#fff' text-anchor='middle'>{initials}</text>"
    "</svg>"
)

_icon_cache = {}  # source URL -> (icon_key, data_uri) or None
_repo_icon_cache = {}  # (owner, repo) -> [candidate URLs]

_RESAMPLING = getattr(Image, "Resampling", Image) if Image else None
_QUANTIZE = getattr(Image, "Quantize", Image) if Image else None

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def sniff_mime(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for sig, mime in MIME_SIGNATURES:
        if data.startswith(sig):
            return mime
    return "application/octet-stream"


def initials_for(name: str) -> str:
    words = [w for w in re.split(r"[-_\s]+", name) if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    word = words[0] if words else name
    camel = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", word)
    if len(camel) >= 2:
        return (camel[0][0] + camel[1][0]).upper()
    return (word[:2] if len(word) >= 2 else word[:1] or "?").upper()


def make_svg_icon(name: str) -> tuple:
    """Generate a small gradient+initials SVG. Always under ICON_MAX_CHARS."""
    idx = int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(ICON_PALETTE)
    c1, c2 = ICON_PALETTE[idx]
    svg = ICON_SVG_TEMPLATE.format(c1=c1, c2=c2, initials=initials_for(name))
    svg_bytes = svg.encode("utf-8")
    icon_key = hashlib.sha256(svg_bytes).hexdigest()
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg_bytes).decode("ascii")
    return icon_key, data_uri


def _hint_score(text: str):
    """Rank a filename/alt-text by how deliberately icon-like it is.
    Lower is better; None means 'not an icon name'."""
    value = text.lower()
    if "icon0" in value:
        return 0
    if "launcher" in value or "appicon" in value or "app-icon" in value:
        return 10
    if "icon" in value:
        return 30
    if "logo" in value:
        return 60
    if "avatar" in value:
        return 70
    if "emblem" in value:
        return 80
    if "brand" in value:
        return 90
    if re.search(r"(?:^|[-_.\s])mark(?:[-_.\s]|$)", value):
        return 100
    if "banner" in value:
        return 110
    return None


def score_icon_path(path: str, context: str = ""):
    """Score a repo file (or README image) as an icon candidate.
    Returns None when the path is not an image or should never be used."""
    lower = path.lower()
    if not lower.endswith(ICON_EXTENSIONS):
        return None
    if any(marker in lower for marker in ICON_SKIP_MARKERS):
        return None

    name = lower.rsplit("/", 1)[-1]
    hint = _hint_score(name)
    if hint is None and context:
        hint = _hint_score(context)
    if hint is None:
        return None

    ext = name.rsplit(".", 1)[-1]
    score = hint + lower.count("/") * 6 + ICON_EXT_BONUS.get(ext, 40)
    if "favicon" in name:
        score += 80  # small, usually 16-32px
    return score


def github_slug(source: str):
    """Pull (owner, repo) out of a GitHub URL, or None."""
    if not source:
        return None
    match = re.search(r"https?://github\.com/([^/?#]+)/([^/?#]+)", source)
    if not match:
        return None
    repo = match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return match.group(1), repo


def resolve_icon_source(value: str) -> str:
    """Normalize a links.txt icon= value into a fetchable image URL."""
    value = value.strip().strip("<>\"'")
    if value.startswith("@"):
        return f"https://github.com/{value[1:].strip()}.png?size={GITHUB_AVATAR_RES}"

    profile = re.fullmatch(r"https?://github\.com/([A-Za-z0-9_.-]+)/?", value)
    if profile:
        return f"https://github.com/{profile.group(1)}.png?size={GITHUB_AVATAR_RES}"

    blob = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/([^/]+)/(.+)$", value)
    if blob:
        owner, repo, branch, path = blob.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    return value


def _readme_image_urls(owner: str, repo: str, branch: str) -> list:
    """Image URLs referenced by the repo README, resolved to raw URLs."""
    try:
        readme = gh_request(f"/repos/{owner}/{repo}/readme")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"  note: could not read README for {owner}/{repo}: {exc}")
        return []
    except (urllib.error.URLError, ValueError) as exc:
        print(f"  note: could not read README for {owner}/{repo}: {exc}")
        return []

    try:
        text = base64.b64decode(readme.get("content", "")).decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return []

    base_dir = posixpath.dirname(readme.get("path") or "")
    found = [(src, alt) for alt, src in MD_IMAGE_RE.findall(text)]
    found += [(src, "") for src in HTML_IMAGE_RE.findall(text)]

    urls = []
    for src, alt in found:
        src = src.strip().strip("<>\"'")
        if not src or src.startswith("data:") or src in ("", "#"):
            continue
        if src.startswith("//"):
            url = "https:" + src
        elif re.match(r"https?://", src):
            if "shields.io" in src or "/badge" in src:
                continue
            url = src
        elif re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", src):
            continue  # mailto:, ftp:, etc.
        else:
            rel = src.lstrip("/") if src.startswith("/") else posixpath.join(base_dir, src)
            rel = posixpath.normpath(rel)
            if rel.startswith(".."):
                continue
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
                f"{urllib.parse.quote(rel)}"
            )
        urls.append((url, alt))
    return urls


def repo_icon_candidates(owner: str, repo: str) -> list:
    """Ordered list of image URLs that could be this repo's icon.

    The repo's real default branch is used (not a hardcoded 'main'), the
    whole file tree is searched (so docs/logo.png, ps5/icon0.png and
    sce_sys/icon0.png are all found), README-embedded images are tried
    next, and the owner's avatar is the last resort."""
    try:
        meta = gh_request(f"/repos/{owner}/{repo}")
        branch = meta.get("default_branch") or "main"
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, AttributeError) as exc:
        print(f"  warning: could not read repo metadata for {owner}/{repo}: {exc}", file=sys.stderr)
        branch = "main"

    scored = []
    try:
        tree = gh_request(
            f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
        )
        for node in tree.get("tree", []):
            if node.get("type") != "blob":
                continue
            path = node.get("path", "")
            score = score_icon_path(path)
            if score is None:
                # A root-level image named after the repo (ProsperoMgr.png)
                # is a weak but real hint - keep it as a late fallback.
                stem, _, ext = path.rpartition(".")
                if (
                    "/" not in path
                    and f".{ext.lower()}" in ICON_EXTENSIONS
                    and stem.lower() == repo.lower()
                ):
                    score = 120
                else:
                    continue
            if score > ICON_MAX_AUTO_SCORE:
                continue
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
                f"{urllib.parse.quote(path)}"
            )
            scored.append((score, path, url))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, AttributeError) as exc:
        print(f"  warning: could not list files for {owner}/{repo}: {exc}", file=sys.stderr)

    for url, alt in _readme_image_urls(owner, repo, branch):
        path = urllib.parse.urlparse(url).path
        score = score_icon_path(path, context=alt)
        if score is None or score > ICON_MAX_AUTO_SCORE:
            continue
        scored.append((score, path, url))

    scored.sort(key=lambda item: (item[0], item[1]))

    urls = []
    for _, _, url in scored:
        if url not in urls:
            urls.append(url)
    urls.append(f"https://github.com/{owner}.png?size={GITHUB_AVATAR_RES}")

    if urls[:-1]:
        print(f"  icon candidates for {owner}/{repo}: {', '.join(urls[:-1][:3])}")
    return urls[:6]


def repo_icon_urls(owner: str, repo: str) -> list:
    key = (owner.lower(), repo.lower())
    if key not in _repo_icon_cache:
        _repo_icon_cache[key] = repo_icon_candidates(owner, repo)
    return _repo_icon_cache[key]


def fetch_bytes(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def image_dimensions(data: bytes):
    if not HAVE_PIL:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def _b64_uri(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _svg_data_uri(data: bytes):
    text = data.decode("utf-8", "ignore")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r">\s+<", "><", text)
    text = re.sub(r"\s+", " ", text).strip()
    raw = text.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), _b64_uri("image/svg+xml", raw)


def _to_tile(img):
    """Shape a non-square image for a square tile.

    Wide art (banners, 3:1) keeps its full frame on a blurred, darkened
    copy of itself, so nothing is cropped and the tile is not half empty.
    Mildly non-square art is center-cropped."""
    width, height = img.size
    if not width or not height:
        return img

    ratio = max(width, height) / min(width, height)
    if ratio <= 1.15:
        return img

    if ratio <= 1.7:
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        return img.crop((left, top, left + side, top + side))

    size = ICON_MAX_DIM
    scale = size / min(width, height)
    backdrop = img.convert("RGB").resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        _RESAMPLING.LANCZOS,
    )
    left = max(0, (backdrop.width - size) // 2)
    top = max(0, (backdrop.height - size) // 2)
    backdrop = backdrop.crop((left, top, left + size, top + size))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(5))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.45)

    foreground = img.copy()
    foreground.thumbnail((size, size), _RESAMPLING.LANCZOS)
    backdrop.paste(
        foreground,
        ((size - foreground.width) // 2, (size - foreground.height) // 2),
        foreground if foreground.mode == "RGBA" else None,
    )
    return backdrop


def _compress_with_pil(data: bytes):
    """Downscale to <= ICON_MAX_DIM and re-encode until it fits the limit."""
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as exc:
        print(f"  warning: could not decode icon image: {exc}", file=sys.stderr)
        return None

    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    rgba = _to_tile(img.convert("RGBA"))
    has_alpha = rgba.mode == "RGBA" and rgba.split()[-1].getextrema()[0] < 250
    base = rgba if has_alpha else rgba.convert("RGB")

    for dim in (ICON_MAX_DIM, 96, 80, 64, 48):
        frame = base.copy()
        frame.thumbnail((dim, dim), _RESAMPLING.LANCZOS)

        attempts = []
        buf = io.BytesIO()
        frame.save(buf, format="PNG", optimize=True)
        attempts.append(("image/png", buf.getvalue()))

        if has_alpha:
            for colors in (128, 64, 32):
                buf = io.BytesIO()
                quantized = frame.quantize(colors=colors, method=_QUANTIZE.FASTOCTREE)
                quantized.save(buf, format="PNG", optimize=True)
                attempts.append(("image/png", buf.getvalue()))
        else:
            for quality in (85, 80, 70):
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=quality, optimize=True)
                attempts.append(("image/jpeg", buf.getvalue()))

        for mime, raw in attempts:
            uri = _b64_uri(mime, raw)
            if len(uri) <= ICON_TARGET_CHARS:
                return hashlib.sha256(raw).hexdigest(), uri

    return None


def encode_icon(data: bytes, declared_mime: str = ""):
    """Turn raw image bytes into (icon_key, data URI), or None if the result
    cannot be stored under ICON_MAX_CHARS."""
    if not data:
        return None

    mime = sniff_mime(data)
    if mime == "application/octet-stream" and declared_mime.startswith("image/"):
        mime = declared_mime.split(";")[0].strip()

    if mime == "image/svg+xml":
        result = _svg_data_uri(data)
        if len(result[1]) <= ICON_TARGET_CHARS:
            return result

    if mime in ("image/png", "image/jpeg"):
        uri = _b64_uri(mime, data)
        if len(uri) <= ICON_TARGET_CHARS:
            return hashlib.sha256(data).hexdigest(), uri

    if HAVE_PIL:
        return _compress_with_pil(data)
    return None


def try_icon_url(url: str, min_dim: int = ICON_MIN_DIM):
    """Download url and return (icon_key, data_uri) or None. Cached by URL."""
    if url in _icon_cache:
        return _icon_cache[url]

    try:
        data, declared_mime = fetch_bytes(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  warning: could not fetch icon {url}: {exc}", file=sys.stderr)
        _icon_cache[url] = None
        return None

    dims = image_dimensions(data)
    if min_dim and dims and min(dims) < min_dim:
        print(f"  skipping {url}: only {dims[0]}x{dims[1]}px")
        _icon_cache[url] = None
        return None

    result = encode_icon(data, declared_mime)
    if result is None:
        hint = "" if HAVE_PIL else " (install Pillow to enable downscaling)"
        print(
            f"  warning: could not fit {url} under {ICON_MAX_CHARS} chars{hint}",
            file=sys.stderr,
        )
    _icon_cache[url] = result
    return result


def icon_for_entry(entry: dict, fields: dict, icons: dict) -> bool:
    """Attach icon_key to entry and populate icons dict. Returns True when a
    real upstream/overridden image was used, False for a generated tile.

    1. `icon=` override from links.txt, if given ('none' disables icons).
    2. Best-scoring image found in the repo (tree, then README).
    3. Repo owner's GitHub avatar.
    4. Generated SVG placeholder - always fits, no external dependencies."""
    name = entry["name"]
    override = (fields.get("icon") or "").strip()

    if override.lower() in ("none", "off", "-"):
        entry.pop("icon_key", None)
        print(f"  icons disabled for {name!r}")
        return False

    result = None
    if override:
        icon_url = resolve_icon_source(override)
        result = try_icon_url(icon_url, min_dim=0)
        if result is None:
            print(f"  warning: icon= for {name!r} could not be used: {icon_url}", file=sys.stderr)
    else:
        slug = github_slug(entry.get("source", "")) or github_slug(entry.get("source_direct", ""))
        if slug:
            for candidate in repo_icon_urls(*slug):
                result = try_icon_url(candidate)
                if result is not None:
                    break

    generated = result is None
    if generated:
        result = make_svg_icon(name)
        print(f"  using generated placeholder icon for {name!r}")

    icon_key, data_uri = result
    entry["icon_key"] = icon_key
    icons[icon_key] = data_uri
    return not generated


def gh_request(path: str) -> dict:
    url = f"{GITHUB_API}{path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_line(line: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    source = parts[0]

    fields = {"source_entry": source}
    positional_keys = ["category", "description", "version"]
    positional_idx = 0

    for part in parts[1:]:
        if "=" in part and part.split("=", 1)[0].strip().lower() in OVERRIDE_KEYS:
            key, _, value = part.partition("=")
            fields[key.strip().lower()] = value.strip()
        else:
            if positional_idx < len(positional_keys):
                fields.setdefault(positional_keys[positional_idx], part)
                positional_idx += 1

    return fields


def read_links(path: str) -> list:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(parse_line(line))
    return entries


def filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path)
    return name or "payload.bin"


def sha256_of_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def checksum_for_asset(asset: dict) -> str:
    digest = asset.get("digest")  # e.g. "sha256:abc123..."
    if digest and digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    try:
        return sha256_of_url(asset["browser_download_url"])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not checksum {asset['browser_download_url']}: {exc}", file=sys.stderr)
        return ""


def release_date(release: dict) -> str:
    raw = release.get("published_at") or release.get("created_at")
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%d")


def build_entries_from_github(fields: dict) -> list:
    owner_repo = fields["source_entry"].split("github:", 1)[1].strip()
    print(f"resolving github:{owner_repo} (latest release)")

    try:
        release = gh_request(f"/repos/{owner_repo}/releases/latest")
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"warning: could not fetch latest release for {owner_repo}: {exc}", file=sys.stderr)
        return []

    assets = release.get("assets", [])
    if not assets:
        print(f"warning: {owner_repo} latest release has no downloadable assets, skipping", file=sys.stderr)
        return []

    assets = [a for a in assets if a["name"].lower().endswith(PAYLOAD_EXTENSIONS)]
    if not assets:
        print(f"warning: {owner_repo} latest release has no .elf/.bin assets, skipping", file=sys.stderr)
        return []

    pattern = fields.get("asset_pattern", "").lower()
    if pattern:
        assets = [a for a in assets if pattern in a["name"].lower()]
        if not assets:
            print(f"warning: no .elf/.bin assets of {owner_repo} match asset_pattern={pattern!r}, skipping", file=sys.stderr)
            return []

    repo_name = owner_repo.split("/", 1)[1]
    category = fields.get("category") or DEFAULT_CATEGORY
    version = fields.get("version") or release.get("tag_name") or ""
    description = fields.get("description") or (release.get("name") or "").strip()
    last_update = release_date(release)
    source_page = f"https://github.com/{owner_repo}/releases"

    entries = []
    for asset in assets:
        direct_url = asset["browser_download_url"]
        entry = {
            "name": fields.get("name") or (repo_name if len(assets) == 1 else asset["name"]),
            "filename": fields.get("filename") or asset["name"],
            "url": direct_url,
            "source": source_page,
            "source_direct": direct_url,
        }
        if description:
            entry["description"] = description
        entry["last_update"] = last_update
        if version:
            entry["version"] = version
        entry["category"] = category
        if fields.get("extract_file"):
            entry["extract_file"] = fields["extract_file"]
        if pattern:
            entry["asset_pattern"] = fields["asset_pattern"]

        checksum = checksum_for_asset(asset)
        if checksum:
            entry["checksum"] = checksum

        entries.append(entry)

    return entries


def build_entry_from_url(fields: dict):
    url = fields["source_entry"]
    name = fields.get("name") or filename_from_url(url).rsplit(".", 1)[0]
    filename = fields.get("filename") or filename_from_url(url)

    if not filename.lower().endswith(PAYLOAD_EXTENSIONS):
        print(f"warning: {url} is not a .elf/.bin file (filename: {filename!r}), skipping", file=sys.stderr)
        return None

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": url,
        "source_direct": url,
    }
    if fields.get("description"):
        entry["description"] = fields["description"]
    if fields.get("version"):
        entry["version"] = fields["version"]
    entry["category"] = fields.get("category") or DEFAULT_CATEGORY
    if fields.get("extract_file"):
        entry["extract_file"] = fields["extract_file"]

    try:
        entry["checksum"] = sha256_of_url(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not checksum {url}: {exc}", file=sys.stderr)

    return entry


def main() -> int:
    if not os.path.exists(LINKS_FILE):
        print(f"error: {LINKS_FILE} not found", file=sys.stderr)
        return 1

    raw_entries = read_links(LINKS_FILE)
    if not raw_entries:
        print(f"error: no payload entries found in {LINKS_FILE}", file=sys.stderr)
        return 1

    payloads = []
    entry_fields = []  # parallel list: originating links.txt fields for each entry
    for fields in raw_entries:
        source = fields["source_entry"]
        if source.startswith("github:"):
            new_entries = build_entries_from_github(fields)
            payloads.extend(new_entries)
            entry_fields.extend([fields] * len(new_entries))
        else:
            print(f"processing {source}")
            entry = build_entry_from_url(fields)
            if entry is not None:
                payloads.append(entry)
                entry_fields.append(fields)

    if not payloads:
        print("error: no payload entries could be resolved", file=sys.stderr)
        return 1

    icons = {}
    placeholder_names = []
    for entry, fields in zip(payloads, entry_fields):
        if not icon_for_entry(entry, fields, icons):
            if entry.get("icon_key"):
                placeholder_names.append(entry["name"])

    catalog = {
        "name": REPO_NAME,
        "icons_url": ICONS_FILE,
        "payloads": payloads,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(ICONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"icons": icons}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {len(payloads)} payload(s) to {OUTPUT_FILE}")
    print(f"wrote {len(icons)} icon(s) to {ICONS_FILE}")
    reused = len(payloads) - len(placeholder_names)
    print(f"icons: {reused} upstream/override, {len(placeholder_names)} generated placeholder(s)")
    if placeholder_names and not HAVE_PIL:
        print(
            "note: install Pillow (pip install -r requirements.txt) to downscale "
            "large icons below the 12,288-char limit",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
