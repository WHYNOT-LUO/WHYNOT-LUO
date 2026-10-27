#!/usr/bin/env python3
"""Add paintings and photos to the GitHub profile gallery.

Drop images into inbox/paintings/ or inbox/photos/, then run this script
(or double-click "Add art.command"). For each image it:
  - converts HEIC, PNG, etc. to JPEG and fixes the rotation,
  - strips all metadata (camera EXIF, GPS location, XMP),
  - writes a web-size copy (art/paintings/ or art/photos/) and a square
    thumbnail (art/thumbs/),
  - records it in art/gallery.json, newest first,
then rebuilds the gallery in README.md and art/README.md, commits and pushes.
Originals are moved to inbox/uploaded/, which never leaves this Mac.

To change a title or the order, edit art/gallery.json and run with --render.
To take a piece down, run with --remove NAME (NAME is the file name without .jpg).
"""
import argparse
import datetime
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox"
UPLOADED = INBOX / "uploaded"
ART = ROOT / "art"
THUMBS = ART / "thumbs"
GALLERY = ART / "gallery.json"
PROFILE_README = ROOT / "README.md"
ART_README = ART / "README.md"

# Section order on the page follows this dict.
KINDS = {
    "paintings": {"heading": "Paintings", "alt": "Painting"},
    "photos": {"heading": "Photography", "alt": "Photograph"},
}
SECTION_TITLE = "Art"
PROFILE_LIMIT = 6  # newest N per section on the profile page; art/README.md shows all
FULL_EDGE = 2000  # long edge of the web-size copy, px
THUMB_EDGE = 600  # square thumbnail, px
START, END = "<!-- ART:START -->", "<!-- ART:END -->"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
CAMERA_NAME = re.compile(
    r"^(img|dsc|dscf|dscn|pxl|mvimg|photo|image|screenshot|screen shot|微信图片|mmexport)[\W_]*\d", re.I
)


def load_gallery():
    if GALLERY.exists():
        return json.loads(GALLERY.read_text(encoding="utf-8"))
    return {"items": []}


def save_gallery(gallery):
    GALLERY.parent.mkdir(parents=True, exist_ok=True)
    GALLERY.write_text(json.dumps(gallery, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def title_from(stem):
    """Readable title from a file name; camera names like IMG_4821 get none."""
    if CAMERA_NAME.match(stem) or re.fullmatch(r"[\W\d_]+", stem):
        return ""
    title = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", stem)).strip()
    return title[:1].upper() + title[1:] if title.islower() else title


def slugify(stem):
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60].strip("-")


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


def add_image(src, kind, digest):
    slug = unique_slug(slugify(src.stem) or f"{kind[:-1]}-{digest[:8]}")
    full_rel = f"art/{kind}/{slug}.jpg"
    thumb_rel = f"art/thumbs/{slug}.jpg"
    im = open_image(src)
    # A CMYK or grey profile would be wrong once the pixels are RGB.
    icc = im.info.get("icc_profile") if im.mode in ("RGB", "RGBA") else None
    im = to_rgb(ImageOps.exif_transpose(im))
    full = im.copy()
    full.thumbnail((FULL_EDGE, FULL_EDGE), Image.LANCZOS)
    save_jpeg(full, ROOT / full_rel, icc, 88)
    save_jpeg(ImageOps.fit(im, (THUMB_EDGE, THUMB_EDGE), Image.LANCZOS), ROOT / thumb_rel, icc, 82)
    return {
        "kind": kind,
        "title": title_from(src.stem),
        "file": full_rel,
        "thumb": thumb_rel,
        "added": datetime.date.today().isoformat(),
        "sha1": digest,
    }


def move_to_uploaded(src):
    UPLOADED.mkdir(parents=True, exist_ok=True)
    dest, n = UPLOADED / src.name, 2
    while dest.exists():
        dest, n = UPLOADED / f"{src.stem}-{n}{src.suffix}", n + 1
    shutil.move(str(src), dest)


def process_inbox(gallery):
    known = {item["sha1"] for item in gallery["items"]}
    added = []
    for kind in KINDS:
        folder = INBOX / kind
        folder.mkdir(parents=True, exist_ok=True)
        for src in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if not src.is_file() or src.name.startswith("."):
                continue
            if src.suffix.lower() not in IMAGE_EXTS:
                print(f"  skipped (not an image): {src.name}")
                continue
            digest = hashlib.sha1(src.read_bytes()).hexdigest()
            if digest in known:
                print(f"  skipped (already in the gallery): {src.name}")
                move_to_uploaded(src)
                continue
            try:
                item = add_image(src, kind, digest)
            except Exception as exc:
                print(f"  could not read {src.name}: {exc}")
                continue
            known.add(digest)
            added.append(item)
            move_to_uploaded(src)
            print(f"  added to {KINDS[kind]['heading']}: {item['title'] or src.name}")
    gallery["items"] = added + gallery["items"]
    return added


def remove_item(gallery, name):
    keep = []
    removed = None
    for item in gallery["items"]:
        if Path(item["file"]).stem == name:
            removed = item
            for rel in (item["file"], item["thumb"]):
                (ROOT / rel).unlink(missing_ok=True)
        else:
            keep.append(item)
    gallery["items"] = keep
    return removed


def grid(items, base):
    tags = []
    for item in items:
        file, thumb = item["file"].removeprefix(base), item["thumb"].removeprefix(base)
        alt = html.escape(item["title"] or KINDS[item["kind"]]["alt"])
        title = f' title="{html.escape(item["title"])}"' if item["title"] else ""
        tags.append(f'<a href="{file}"><img src="{thumb}" width="32%" alt="{alt}"{title}></a>')
    return "<p>\n" + "\n".join(tags) + "\n</p>"


def render_sections(items, limit, base, heading_level):
    parts, hidden = [], 0
    for kind, meta in KINDS.items():
        of_kind = [item for item in items if item["kind"] == kind]
        if not of_kind:
            continue
        shown = of_kind[:limit] if limit else of_kind
        hidden += len(of_kind) - len(shown)
        parts.append(f"{'#' * heading_level} {meta['heading']}\n\n{grid(shown, base)}")
    return parts, hidden


def write_readmes(gallery):
    items = gallery["items"]
    parts, hidden = render_sections(items, PROFILE_LIMIT, "", 3)
    if parts:
        block = f"## {SECTION_TITLE}\n\n" + "\n\n".join(parts)
        if hidden:
            block += f'\n\n<sub><a href="art/README.md">See all {len(items)} pieces</a></sub>'
        block = f"{START}\n{block}\n{END}"
    else:
        block = f"{START}\n{END}"

    text = PROFILE_README.read_text(encoding="utf-8") if PROFILE_README.exists() else ""
    if START in text and END in text:
        head, rest = text.split(START, 1)
        text = head + block + rest.split(END, 1)[1]
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    PROFILE_README.write_text(text, encoding="utf-8")

    parts, _ = render_sections(items, None, "art/", 2)
    ART_README.parent.mkdir(parents=True, exist_ok=True)
    ART_README.write_text(
        f"# {SECTION_TITLE}\n\nClick a picture for the full size.\n\n" + "\n\n".join(parts) + "\n", encoding="utf-8"
    )


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
    if has_origin():
        if git(*credential_args(), "pull", "--ff-only", "--quiet", check=False).returncode != 0:
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
    parser.add_argument("--render", action="store_true", help="only rebuild the READMEs from art/gallery.json")
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
    write_readmes(gallery)
    publish(summary, push=not args.no_push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
