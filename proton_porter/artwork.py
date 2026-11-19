"""Library artwork from SteamGridDB (needs a free API key from steamgriddb.com/profile/preferences/api)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.steamgriddb.com/api/v2"
UA = "proton-porter"

# Steam's file names in userdata/<id>/config/grid/ for a shortcut with this appid.
SLOTS = [
    ("grids", {"dimensions": "600x900"}, "{appid}p"),        # library cover
    ("grids", {"dimensions": "920x430,460x215"}, "{appid}"),  # wide capsule
    ("heroes", {}, "{appid}_hero"),                           # banner on the game page
    ("logos", {}, "{appid}_logo"),
]


class ArtworkError(Exception):
    pass


def _get(path: str, key: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ArtworkError("SteamGridDB rejected the API key") from error
        raise ArtworkError(f"SteamGridDB error {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ArtworkError(f"Couldn't reach SteamGridDB ({error})") from error
    if not body.get("success"):
        raise ArtworkError("SteamGridDB returned an error")
    return body.get("data") or []


def find_game(name: str, key: str) -> tuple[int, str] | None:
    hits = _get(f"/search/autocomplete/{urllib.parse.quote(name, safe='')}", key)
    if not hits:
        return None
    return hits[0]["id"], hits[0]["name"]


def download(name: str, appid: int, grid_dir: Path, key: str) -> tuple[str, list[str]]:
    """Fetch cover, capsule, hero and logo. Returns (matched name, saved files)."""
    found = find_game(name, key)
    if not found:
        raise ArtworkError(f"No SteamGridDB match for “{name}”")
    game_id, matched = found
    grid_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for kind, params, stem in SLOTS:
        try:
            items = _get(f"/{kind}/game/{game_id}", key, params)
        except ArtworkError:
            continue
        items = [i for i in items if not i.get("nsfw") and not i.get("humor")] or items
        if not items:
            continue
        url = items[0]["url"]
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        base = stem.format(appid=appid)
        for old in grid_dir.glob(base + ".*"):
            old.unlink()
        dest = grid_dir / f"{base}{ext}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            saved.append(dest.name)
        except (urllib.error.URLError, TimeoutError):
            continue
    return matched, saved
