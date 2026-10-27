#!/usr/bin/env python3
"""Add paintings and photos to the GitHub profile gallery.

Drop images into inbox/, then run this script (or double-click "Add art.command"):
  inbox/paintings/          paintings and drawings, hung on the gallery wall
  inbox/photos/<Album>/     photos; the folder name becomes the album name
  inbox/photos/             loose photos go to the "Everyday" album
Black-and-white photos also join the "Black & white" album.

Each image is converted to JPEG with its rotation fixed and all metadata
(camera EXIF, GPS location, XMP) stripped. A web-size copy and a square
thumbnail go into art/, and the piece is recorded in art/gallery.json.
Then the gallery wall, album cards and film strips are redrawn (art/views/,
light and dark versions), README.md, art/README.md and art/albums/*.md are
rebuilt, and everything is committed and pushed. Originals move to
inbox/uploaded/, which never leaves this Mac.

In art/gallery.json you can give a piece a "title", reorder "albums", or set an
album's "cover" to a list of names (file names without .jpg): the first 3 make
the album card, the first 6 the film strip. Run with --render afterwards.
To take a piece down: --remove NAME.
"""
import argparse
import datetime
import hashlib
import html
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox"
UPLOADED = INBOX / "uploaded"
ART = ROOT / "art"
THUMBS = ART / "thumbs"
VIEWS = ART / "views"
ALBUM_PAGES = ART / "albums"
GALLERY = ART / "gallery.json"
PROFILE_README = ROOT / "README.md"
ART_README = ART / "README.md"

PAINTINGS, PHOTOS = "paintings", "photos"
KINDS = (PAINTINGS, PHOTOS)
SECTION_TITLE = "Art"
PAINTINGS_HEADING = "Sketchbook"
PHOTOS_HEADING = "Photography"
DEFAULT_ALBUM = "Everyday"
BW_ALBUM = "Black & white"
WALL_SIZE = 4  # paintings per wall row; the profile shows the newest row
FULL_EDGE = 2000  # long edge of the web-size copy, px
THUMB_EDGE = 600  # square thumbnail, px
START, END = "<!-- ART:START -->", "<!-- ART:END -->"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
CAMERA_NAME = re.compile(
    r"^(img|dsc|dscf|dscn|pxl|mvimg|photo|image|screenshot|screen shot|微信图片|mmexport)[\W_]*\d", re.I
)

AVENIR = "/System/Library/Fonts/Avenir Next.ttc"
FONTS = {
    "bold": (AVENIR, 2),  # Demi Bold
    "regular": (AVENIR, 5),  # Medium
    "film": ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
}
# Colours match GitHub's light and dark themes so the pictures sit on the page.
THEMES = {
    "light": {
        "page": (255, 255, 255), "card": (246, 248, 250), "border": (208, 215, 222),
        "title": (31, 35, 40), "sub": (89, 99, 110),
        "wall": ((243, 239, 232), (222, 217, 209)), "glow": None,
        "film": (18, 17, 16), "hole": (255, 255, 255),
    },
    "dark": {
        "page": (13, 17, 23), "card": (22, 27, 34), "border": (48, 54, 61),
        "title": (230, 237, 243), "sub": (145, 152, 161),
        "wall": ((46, 44, 41), (24, 23, 22)), "glow": (255, 236, 200),
        "film": (34, 32, 30), "hole": (236, 228, 214),  # backlit, as on a light box
    },
}
FILM_EMPTY, FILM_AMBER = (52, 50, 47), (232, 163, 61)


# ---------- gallery data ----------

def load_gallery():
    gallery = json.loads(GALLERY.read_text(encoding="utf-8")) if GALLERY.exists() else {}
    gallery.setdefault("albums", [])
    gallery.setdefault("items", [])
    return gallery


def save_gallery(gallery):
    GALLERY.parent.mkdir(parents=True, exist_ok=True)
    GALLERY.write_text(json.dumps(gallery, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def name_of(item):
    return Path(item["file"]).stem


def ensure_album(gallery, name):
    if any(album["name"] == name for album in gallery["albums"]):
        return
    taken = {album["slug"] for album in gallery["albums"]}
    base = slugify(name) or "album"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    gallery["albums"].append({"name": name, "slug": slug, "cover": []})


# ---------- importing ----------

def title_from(stem):
    """Readable title from a file name; camera names like IMG_4821 get none."""
    if CAMERA_NAME.match(stem) or re.fullmatch(r"[\W\d_]+", stem):
        return ""
    title = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", stem)).strip()
    return title[:1].upper() + title[1:] if title.islower() else title


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


def unique_slug(base):
    slug, n = base, 2
    while (THUMBS / f"{slug}.jpg").exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def open_image(src):
    if src.suffix.lower() in {".heic", ".heif"}:
        # Pillow can't read HEIC; macOS sips can.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "converted.jpg"
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "best", str(src), "--out", str(out)],
                check=True,
                capture_output=True,
            )
            im = Image.open(out)
            im.load()
            return im
    im = Image.open(src)
    im.load()
    return im


def taken_date(im, src):
    exif = im.getexif()
    stamp = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
    try:
        return datetime.datetime.strptime(str(stamp).strip()[:10], "%Y:%m:%d").date().isoformat()
    except ValueError:
        return datetime.date.fromtimestamp(src.stat().st_mtime).isoformat()


def is_black_and_white(im):
    small = im.convert("RGB").resize((120, 120))
    r, g, b = small.split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                                 ImageChops.darker(ImageChops.darker(r, g), b))
    hist = spread.histogram()
    return sum(i * count for i, count in enumerate(hist)) / sum(hist) < 4


def to_rgb(im):
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel("A"))
        return flat
    return im.convert("RGB")


def save_jpeg(im, path, icc, quality):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = im.copy()
    im.info = {}  # drop EXIF/XMP/GPS; only the colour profile is written back
    im.save(path, "JPEG", quality=quality, optimize=True, progressive=True, icc_profile=icc)


def add_image(src, kind, digest, album):
    slug = unique_slug(slugify(src.stem) or f"{kind[:-1]}-{digest[:8]}")
    full_rel = f"art/{kind}/{slug}.jpg"
    thumb_rel = f"art/thumbs/{slug}.jpg"
    im = open_image(src)
    taken = taken_date(im, src)
    # A CMYK or grey profile would be wrong once the pixels are RGB.
    icc = im.info.get("icc_profile") if im.mode in ("RGB", "RGBA") else None
    im = to_rgb(ImageOps.exif_transpose(im))
    full = im.copy()
    full.thumbnail((FULL_EDGE, FULL_EDGE), Image.LANCZOS)
    save_jpeg(full, ROOT / full_rel, icc, 88)
    save_jpeg(ImageOps.fit(im, (THUMB_EDGE, THUMB_EDGE), Image.LANCZOS), ROOT / thumb_rel, icc, 82)
    item = {
        "kind": kind,
        "title": title_from(src.stem),
        "file": full_rel,
        "thumb": thumb_rel,
        "taken": taken,
        "added": datetime.date.today().isoformat(),
        "sha1": digest,
    }
    if kind == PHOTOS:
        item["albums"] = [album] + ([BW_ALBUM] if album != BW_ALBUM and is_black_and_white(im) else [])
    return item


def move_to_uploaded(src):
    UPLOADED.mkdir(parents=True, exist_ok=True)
    dest, n = UPLOADED / src.name, 2
    while dest.exists():
        dest, n = UPLOADED / f"{src.stem}-{n}{src.suffix}", n + 1
    shutil.move(str(src), dest)


def remove_empty_folders(folder):
    for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        (sub / ".DS_Store").unlink(missing_ok=True)
        if not any(sub.iterdir()):
            sub.rmdir()


def process_inbox(gallery):
    known = {item["sha1"] for item in gallery["items"]}
    added = []
    for kind in KINDS:
        folder = INBOX / kind
        folder.mkdir(parents=True, exist_ok=True)
        for src in sorted(folder.rglob("*"), key=lambda p: str(p).lower()):
            parts = src.relative_to(folder).parts
            if not src.is_file() or any(part.startswith(".") for part in parts):
                continue
            if src.suffix.lower() not in IMAGE_EXTS:
                print(f"  skipped (not an image): {src.name}")
                continue
            digest = hashlib.sha1(src.read_bytes()).hexdigest()
            if digest in known:
                print(f"  skipped (already in the gallery): {src.name}")
                move_to_uploaded(src)
                continue
            album = (parts[0] if len(parts) > 1 else DEFAULT_ALBUM) if kind == PHOTOS else None
            try:
                item = add_image(src, kind, digest, album)
            except Exception as exc:
                print(f"  could not read {src.name}: {exc}")
                continue
            for name in item.get("albums", []):
                ensure_album(gallery, name)
            known.add(digest)
            added.append(item)
            move_to_uploaded(src)
            where = " + ".join(item["albums"]) if kind == PHOTOS else PAINTINGS_HEADING
            print(f"  added to {where}: {item['title'] or src.name}")
        remove_empty_folders(folder)
    gallery["items"] = sorted(added + gallery["items"], key=lambda item: item["taken"], reverse=True)
    return added


def remove_item(gallery, name):
    removed = next((item for item in gallery["items"] if name_of(item) == name), None)
    if not removed:
        return None
    for rel in (removed["file"], removed["thumb"]):
        (ROOT / rel).unlink(missing_ok=True)
    gallery["items"].remove(removed)
    in_use = {album for item in gallery["items"] for album in item.get("albums", [])}
    gallery["albums"] = [album for album in gallery["albums"] if album["name"] in in_use]
    for album in gallery["albums"]:
        album["cover"] = [stem for stem in album.get("cover", []) if stem != name]
    return removed


# ---------- drawing ----------

def font(style, size):
    path, index = FONTS[style]
    try:
        return ImageFont.truetype(path, size, index=index)
    except OSError:
        return ImageFont.load_default(size)


def load(item):
    return Image.open(ROOT / item["file"]).convert("RGB")


def month(iso):
    return datetime.date.fromisoformat(iso).strftime("%b %Y")


def date_range(items):
    dates = sorted(item["taken"] for item in items)
    first, last = month(dates[0]), month(dates[-1])
    if first == last:
        return first
    if dates[0][:4] == dates[-1][:4]:
        return f"{first[:3]} – {last}"
    return f"{first} – {last}"


def fit_text(draw, text, fnt, width):
    if draw.textlength(text, font=fnt) <= width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def drop_shadow(canvas, box, radius, offset, alpha):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(layer).rectangle([x0 + offset, y0 + offset, x1 + offset, y1 + offset], fill=(0, 0, 0, alpha))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius)))


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius, fill=255)
    return mask


def painting_label(item, number):
    return item["title"] or f"No. {number}"


def draw_wall(pieces, theme):
    """pieces: [(item, number)], hung left to right."""
    t = THEMES[theme]
    W, H = 1600, 760
    canvas = Image.new("RGBA", (W, H))
    draw = ImageDraw.Draw(canvas)
    (r0, g0, b0), (r1, g1, b1) = t["wall"]
    for y in range(H):
        f = y / H
        draw.line([(0, y), (W, y)], fill=(int(r0 + (r1 - r0) * f), int(g0 + (g1 - g0) * f), int(b0 + (b1 - b0) * f), 255))
    slot = W / max(len(pieces), 3)
    left = (W - slot * len(pieces)) / 2
    mat, frame = 28, 10
    for i, (item, number) in enumerate(pieces):
        art = ImageOps.contain(load(item), (int(slot) - 2 * (mat + frame) - 60, 420), Image.LANCZOS)
        w, h = art.width + 2 * (mat + frame), art.height + 2 * (mat + frame)
        cx = int(left + slot * i + slot / 2)
        x, y = cx - w // 2, 60 + (500 - h) // 2
        if t["glow"]:  # picture light above each piece on the dark wall
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(glow).ellipse([cx - w * 0.75, y - 120, cx + w * 0.75, y + h * 0.9], fill=t["glow"] + (46,))
            canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))
        drop_shadow(canvas, (x, y, x + w, y + h), 16, 10, 110 if theme == "light" else 160)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([x, y, x + w, y + h], fill=(30, 28, 26, 255))
        draw.rectangle([x + frame, y + frame, x + w - frame, y + h - frame], fill=(250, 249, 246, 255))
        canvas.paste(art, (x + frame + mat, y + frame + mat))
        name, when = painting_label(item, number), month(item["taken"])
        name_font, when_font = font("bold", 26), font("regular", 20)
        name = fit_text(draw, name, name_font, slot - 60)
        lw = int(max(draw.textlength(name, font=name_font), draw.textlength(when, font=when_font))) + 36
        lx, ly = cx - lw // 2, 610
        draw.rectangle([lx, ly, lx + lw, ly + 74], fill=(252, 251, 249, 255), outline=(214, 209, 201, 255))
        draw.text((lx + 18, ly + 8), name, font=name_font, fill=(40, 38, 36))
        draw.text((lx + 18, ly + 42), when, font=when_font, fill=(118, 114, 108))
    return canvas.convert("RGB")


def draw_card(album, photos, theme):
    t = THEMES[theme]
    CW, CH, pad, gap = 480, 680, 16, 8
    canvas = Image.new("RGB", (CW, CH), t["page"])
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([0, 0, CW - 1, CH - 1], 22, fill=t["card"], outline=t["border"], width=2)
    picks = cover(album, photos, 3)
    inner, area = CW - 2 * pad, 540
    if len(picks) == 1:
        boxes = [(pad, pad, inner, area)]
    else:
        top = 360
        small_w = inner if len(picks) == 2 else (inner - gap) // 2
        boxes = [(pad, pad, inner, top)] + [
            (pad + i * (small_w + gap), pad + top + gap, small_w, area - top - gap) for i in range(len(picks) - 1)
        ]
    for item, (x, y, w, h) in zip(picks, boxes):
        tile = ImageOps.fit(load(item), (w, h), Image.LANCZOS)
        canvas.paste(tile, (x, y), rounded(tile, 12))
    title_font, sub_font = font("bold", 40), font("regular", 28)
    draw.text((pad + 4, pad + area + 14), fit_text(draw, album["name"], title_font, inner - 8), font=title_font, fill=t["title"])
    count = f"{len(photos)} photo{'s' if len(photos) != 1 else ''} · {date_range(photos)}"
    draw.text((pad + 4, pad + area + 70), count, font=sub_font, fill=t["sub"])
    return canvas


def draw_strip(album, photos, theme):
    frames, fw, fh, gap, pad = 6, 240, 320, 24, 84
    W, H = frames * fw + (frames + 1) * gap, fh + 2 * pad
    t = THEMES[theme]
    canvas = Image.new("RGB", (W, H), t["film"])
    draw = ImageDraw.Draw(canvas)
    for x in range(12, W - 20, 34):
        for y in (16, H - 38):
            draw.rounded_rectangle([x, y, x + 16, y + 22], 4, fill=t["hole"])
    film = font("film", 22)
    edge = f"{album['name'].upper()}  ·  {date_range(photos).upper()}"
    draw.text((gap, 46), fit_text(draw, edge, film, W - 2 * gap), font=film, fill=FILM_AMBER)
    picks = cover(album, photos, frames)
    for i in range(frames):
        x = gap + i * (fw + gap)
        if i < len(picks):
            canvas.paste(ImageOps.fit(load(picks[i]), (fw, fh), Image.LANCZOS), (x, pad))
        else:
            draw.rectangle([x, pad, x + fw - 1, pad + fh - 1], fill=FILM_EMPTY)
        draw.text((x + fw // 2 - 14, H - 74), f"{i + 1}A", font=film, fill=FILM_AMBER)
    return canvas


def cover(album, photos, n):
    """The album's chosen cover pieces first, then picks spread across the album."""
    by_name = {name_of(item): item for item in photos}
    picks = [by_name[stem] for stem in album.get("cover", []) if stem in by_name]
    if len(photos) <= n:
        spread = photos
    else:
        spread = [photos[round(i * (len(photos) - 1) / (n - 1))] for i in range(n)] if n > 1 else photos[:1]
    return (picks + [item for item in spread + photos if item not in picks])[:n] if photos else []


# ---------- pages ----------

def picture(stem, alt, width, page_dir):
    """<picture> that swaps to the dark version in GitHub's dark theme."""
    light = posixpath.relpath(f"art/views/{stem}-light.jpg", page_dir)
    dark = posixpath.relpath(f"art/views/{stem}-dark.jpg", page_dir)
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{dark}">'
            f'<img src="{light}" width="{width}" alt="{html.escape(alt)}"></picture>')


def link(target, inner, page_dir):
    return f'<a href="{posixpath.relpath(target, page_dir)}">{inner}</a>'


def save_view(stem, draw, *args):
    for theme in THEMES:
        save_jpeg(draw(*args, theme), VIEWS / f"{stem}-{theme}.jpg", None, 86)


def render(gallery):
    shutil.rmtree(VIEWS, ignore_errors=True)
    shutil.rmtree(ALBUM_PAGES, ignore_errors=True)
    VIEWS.mkdir(parents=True)

    paintings = sorted((i for i in gallery["items"] if i["kind"] == PAINTINGS), key=lambda i: i["taken"])
    numbered = [(item, n + 1) for n, item in enumerate(paintings)]
    rows = [numbered[i:i + WALL_SIZE] for i in range(0, len(numbered), WALL_SIZE)]
    for r, row in enumerate(rows):
        save_view(f"wall-{r + 1}", draw_wall, row)
    latest = numbered[-WALL_SIZE:]
    latest_view = f"wall-{len(rows)}"
    if rows and latest != rows[-1]:
        latest_view = "wall-latest"
        save_view(latest_view, draw_wall, latest)

    albums = []
    for album in gallery["albums"]:
        photos = sorted((i for i in gallery["items"] if album["name"] in i.get("albums", [])), key=lambda i: i["taken"])
        if photos:
            albums.append((album, photos))
            save_view(f"card-{album['slug']}", draw_card, album, photos)
            save_view(f"strip-{album['slug']}", draw_strip, album, photos)

    def wall_alt(pieces):
        return "Paintings on a gallery wall: " + ", ".join(painting_label(i, n) for i, n in pieces)

    def cards(page_dir):
        return "<p>\n" + "\n".join(
            link(f"art/albums/{a['slug']}.md",
                 picture(f"card-{a['slug']}", f"{a['name']} album, {len(p)} photos", "23%", page_dir), page_dir)
            for a, p in albums
        ) + "\n</p>"

    # Profile: newest wall row, the first album as a film strip, then every album card.
    parts = []
    if paintings:
        parts.append(f"### {PAINTINGS_HEADING}\n\n"
                     + link("art/README.md", picture(latest_view, wall_alt(latest), "100%", "."), "."))
    if albums:
        album, _ = albums[0]
        strip = picture(f"strip-{album['slug']}", f"Film strip from the {album['name']} album", "100%", ".")
        parts.append(f"### {PHOTOS_HEADING}\n\n" + link(f"art/albums/{album['slug']}.md", strip, ".") + "\n\n" + cards("."))
    block = f"{START}\n## {SECTION_TITLE}\n\n" + "\n\n".join(parts) + f"\n{END}" if parts else f"{START}\n{END}"
    text = PROFILE_README.read_text(encoding="utf-8") if PROFILE_README.exists() else ""
    if START in text and END in text:
        head, rest = text.split(START, 1)
        text = head + block + rest.split(END, 1)[1]
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    PROFILE_README.write_text(text, encoding="utf-8")

    # art/README.md: every wall row with full-size links, then every album.
    page = [f"# {SECTION_TITLE}"]
    if paintings:
        page.append(f"## {PAINTINGS_HEADING}")
        page += [picture(f"wall-{r + 1}", wall_alt(row), "100%", "art") for r, row in enumerate(rows)]
        page.append("Full size: " + " · ".join(
            f"[{painting_label(i, n)} ({month(i['taken'])})]({posixpath.relpath(i['file'], 'art')})" for i, n in numbered))
    if albums:
        page += [f"## {PHOTOS_HEADING}", cards("art")]
    ART_README.write_text("\n\n".join(page) + "\n", encoding="utf-8")

    # art/albums/<slug>.md: film strip, then every photo, oldest first.
    ALBUM_PAGES.mkdir(parents=True, exist_ok=True)
    here = "art/albums"
    for album, photos in albums:
        tiles = []
        for i in photos:
            alt = html.escape(i["title"] or "Photograph")
            title = f' title="{html.escape(i["title"])}"' if i["title"] else ""
            tiles.append(link(i["file"], f'<img src="{posixpath.relpath(i["thumb"], here)}" width="32%" alt="{alt}"{title}>', here))
        count = f"{len(photos)} photo{'s' if len(photos) != 1 else ''} · {date_range(photos)}"
        others = " · ".join(link(f"art/albums/{a['slug']}.md", html.escape(a["name"]), here) for a, _ in albums if a is not album)
        (ALBUM_PAGES / f"{album['slug']}.md").write_text("\n\n".join([
            f"# {album['name']}",
            count,
            picture(f"strip-{album['slug']}", f"Film strip from the {album['name']} album", "100%", here),
            "<p>\n" + "\n".join(tiles) + "\n</p>",
            f"<sub>{link('art/README.md', 'All art', here)}{' · Other albums: ' + others if others else ''}</sub>",
        ]) + "\n", encoding="utf-8")


# ---------- git ----------

def git(*args, check=True, **kwargs):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, **kwargs)


def has_origin():
    return (ROOT / ".git").exists() and git("remote", "get-url", "origin", check=False, capture_output=True).returncode == 0


def gh_path():
    gh = shutil.which("gh") or str(Path.home() / ".local/bin/gh")
    return gh if Path(gh).exists() else None


def credential_args():
    # The keychain token may lack scopes; the gh CLI's token works for this account.
    gh = gh_path()
    return ["-c", "credential.helper=", "-c", f"credential.helper=!{gh} auth git-credential"] if gh else []


def connect_github():
    """First run: create the special <login>/<login> repo whose README shows on the profile."""
    gh = gh_path()
    if not gh:
        print("Not connected to GitHub (gh not found); the commit stays on this Mac.")
        return False
    login = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True)
    if login.returncode != 0:
        print("gh is not logged in (run: gh auth login); the commit stays on this Mac.")
        return False
    name = login.stdout.strip()
    created = subprocess.run(
        [gh, "repo", "create", f"{name}/{name}", "--public", "--source", str(ROOT), "--remote", "origin",
         "--description", "Paintings and photographs on my GitHub profile"],
        cwd=ROOT,
    )
    if created.returncode != 0:  # most likely it already exists
        git("remote", "add", "origin", f"https://github.com/{name}/{name}.git", check=False)
    return has_origin()


def pull():
    tracking = git("rev-parse", "--abbrev-ref", "@{u}", check=False, capture_output=True)
    if has_origin() and tracking.returncode == 0:  # nothing to pull before the first push
        if git(*credential_args(), "pull", "--ff-only", "--quiet", check=False, capture_output=True).returncode != 0:
            print("  warning: could not pull the latest version from GitHub; continuing with the local copy")


def push_pending():
    """Push local commits, including ones left over from an earlier failed push."""
    if not (ROOT / ".git").exists():
        return
    if not has_origin() and not (load_gallery()["items"] and connect_github()):
        return  # never create the public repo for an empty gallery
    ahead = git("rev-list", "--count", "@{u}..HEAD", check=False, capture_output=True, text=True)
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return
    if git(*credential_args(), "push", "--quiet", "-u", "origin", "HEAD", check=False).returncode == 0:
        print("Pushed. Your GitHub profile updates in a few seconds.")
    else:
        print("Push failed. The commit is saved; run this again when online.")


def publish(summary, push):
    if not (ROOT / ".git").exists():
        print("Not a git repository; nothing committed.")
        return
    git("add", "-A", "art", "README.md")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Nothing changed.")
    else:
        git("commit", "--quiet", "-m", summary)
        print(f"Committed: {summary}")
    if push:
        push_pending()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--render", action="store_true", help="only redraw and rebuild the pages from art/gallery.json")
    parser.add_argument("--remove", metavar="NAME", help="take one piece down (file name without .jpg)")
    parser.add_argument("--no-push", action="store_true", help="commit but don't push")
    args = parser.parse_args()

    gallery = load_gallery()
    if args.remove:
        removed = remove_item(gallery, args.remove)
        if not removed:
            sys.exit(f"No piece named {args.remove!r}. Names are the file names in art/thumbs/ without .jpg.")
        summary = f"Remove {removed['title'] or args.remove} from the gallery"
    elif args.render:
        summary = "Update art gallery"
    else:
        pull()
        print("Reading inbox/paintings and inbox/photos ...")
        added = process_inbox(gallery)
        if not added:
            print("No new images. Put pictures in inbox/paintings or inbox/photos, then run this again.")
            if gallery["items"] and not args.no_push:
                push_pending()
            return 2
        summary = f"Add {len(added)} new piece{'s' if len(added) > 1 else ''} to the gallery"

    save_gallery(gallery)
    print("Drawing the gallery wall, album cards and film strips ...")
    render(gallery)
    publish(summary, push=not args.no_push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
