#!/usr/bin/env python3
"""Refresh the YouTube Korea daily chart and Spotify track ids.

Fetches https://kworb.net/youtube/insights/kr_daily.html and writes songs.json.
The Instagram Reels list is kept; only Spotify ids are added to those rows.
Spotify ids come from kworb country charts (no Spotify API credentials).
Mappings are cached in spotify-cache.json.

A failed fetch, a broken table, or a partial chart of fewer than 30 songs
exits without writing. kworb currently publishes the whole KR daily table
in one closed <tbody>. That full table is kept even when it is shorter
than 50, as long as the ranks are 1..N and N is at least 15. A short,
unclosed, or non-consecutive table is treated as a failed parse.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SONGS_PATH = ROOT / "songs.json"
CACHE_PATH = ROOT / "spotify-cache.json"
KST = timezone(timedelta(hours=9))

YT_URL = "https://kworb.net/youtube/insights/kr_daily.html"
SPOTIFY_URLS = (
    ("kr_daily", "https://kworb.net/spotify/country/kr_daily.html"),
    ("kr_weekly", "https://kworb.net/spotify/country/kr_weekly.html"),
    ("global_daily", "https://kworb.net/spotify/country/global_daily.html"),
)
SOURCE_RANK = {"kr_daily": 0, "kr_weekly": 1, "global_daily": 2}
SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
MIN_PARTIAL = 30
MIN_COMPLETE = 15
TOP_N = 50

ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
TRACK_RE = re.compile(
    r'href="(?:\.\./|/spotify/)?track/([A-Za-z0-9]{22})\.html"[^>]*>(.*?)</a>',
    re.I | re.S,
)
ARTIST_RE = re.compile(
    r'href="(?:\.\./|/spotify/)?artist/[A-Za-z0-9]+\.html"[^>]*>(.*?)</a>',
    re.I | re.S,
)
YT_ID_RE = re.compile(r"watch\?v=([\w-]{11})")


class ChartError(Exception):
    pass


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "dongjin-music-chart/1.0"})
    with urlopen(req, timeout=45) as resp:
        data = resp.read()
    return data.decode("utf-8", "replace")


def strip_tags(value: str) -> str:
    return unescape(TAG_RE.sub("", value or "")).replace("\xa0", " ").strip()


def norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.casefold().replace("’", "'").replace("‘", "'")
    text = text.replace("`", "'").replace("´", "'")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\b(feat|ft|featuring|with)\b\.?", " ", text)
    text = text.replace("'", "")
    text = re.sub(r"[^0-9a-z가-힣\u3040-\u30ff\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def variants(value: str) -> list[str]:
    """Normalized forms, including names kept in parentheses (IU, Unknown Planet)."""
    raw = unicodedata.normalize("NFKC", value or "")
    inners = [a or b for a, b in re.findall(r"\(([^)]*)\)|\[([^\]]*)\]", raw)]
    outer = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", raw)
    pieces = [outer]
    for inner in inners:
        if re.match(r"\s*(feat|ft|featuring|with)\b", inner, re.I):
            continue
        pieces.append(inner)
    found = []
    for piece in pieces:
        text = norm(piece)
        if text and text not in found:
            found.append(text)
    return found


def split_artist_title(label: str) -> tuple[str, str]:
    label = re.sub(r"\s+", " ", label).strip()
    if " - " in label:
        artist, title = label.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", label


def artist_ok(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    stop = {"the", "a", "of", "and", "x"}
    lt = {tok for tok in left.split() if tok not in stop}
    rt = {tok for tok in right.split() if tok not in stop}
    return bool(lt & rt)


def parse_youtube_daily(html: str) -> tuple[str, list[dict]]:
    date_m = re.search(r"Daily Chart\s*-\s*(\d{4})/(\d{2})/(\d{2})", html)
    if not date_m:
        raise ChartError("chart date not found")
    chart_date = f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}"
    table_m = re.search(
        r'<table[^>]*id="dailytable"[^>]*>(.*?)</table>',
        html,
        re.I | re.S,
    )
    if not table_m:
        raise ChartError("daily table not found")
    body_m = re.search(r"<tbody>(.*)</tbody>", table_m.group(1), re.I | re.S)
    if not body_m:
        raise ChartError("daily table body not found")
    rows = []
    for row_html in ROW_RE.findall(body_m.group(1)):
        cells = CELL_RE.findall(row_html)
        if len(cells) < 3:
            continue
        rank_m = re.search(r"\d+", strip_tags(cells[0]))
        if not rank_m:
            continue
        artist, title = split_artist_title(strip_tags(cells[2]))
        if not title:
            continue
        item = {
            "rank": int(rank_m.group(0)),
            "title": title,
            "artist": artist,
        }
        yt_m = YT_ID_RE.search(row_html)
        if yt_m:
            item["youtubeId"] = yt_m.group(1)
        rows.append(item)
    rows.sort(key=lambda row: row["rank"])
    ranks = [row["rank"] for row in rows]
    if ranks != list(range(1, len(rows) + 1)):
        raise ChartError(f"non-consecutive ranks: {ranks[:8]}")
    # A broken or tiny table must not replace songs.json. kworb's KR daily
    # page publishes one closed table (often about 20 rows, not 50). That
    # full 1..N table is the chart. Fewer than MIN_COMPLETE rows is a
    # failed parse and leaves the file untouched.
    if len(rows) < MIN_COMPLETE:
        raise ChartError(f"only {len(rows)} songs")
    return chart_date, rows[:TOP_N]


def parse_spotify(html: str, source: str) -> list[dict]:
    tracks = []
    for row_html in ROW_RE.findall(html):
        track_m = TRACK_RE.search(row_html)
        if not track_m:
            continue
        spotify_id, title_html = track_m.group(1), track_m.group(2)
        if not SPOTIFY_ID.fullmatch(spotify_id):
            continue
        title = strip_tags(title_html)
        artists = []
        for artist_html in ARTIST_RE.findall(row_html):
            name = strip_tags(artist_html)
            if name and name not in artists:
                artists.append(name)
        if not title:
            continue
        artist = ", ".join(artists)
        tracks.append(
            {
                "spotifyId": spotify_id,
                "title": title,
                "artist": artist,
                "title_norms": variants(title),
                "artist_norms": variants(artist),
                "source": source,
            }
        )
    return tracks


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def cache_key(title: str, artist: str) -> str:
    return f"{norm(title)}|{norm(artist)}"


def choose(cands: list[dict]) -> dict | None:
    if not cands:
        return None
    cands = sorted(cands, key=lambda row: (SOURCE_RANK.get(row["source"], 9), row["title"]))
    return cands[0]


def match_track(title: str, artist: str, index: list[dict], cache: dict) -> dict | None:
    key = cache_key(title, artist)
    cached = cache.get(key) or {}
    cached_id = cached.get("spotifyId")
    if isinstance(cached_id, str) and SPOTIFY_ID.fullmatch(cached_id):
        return {
            "spotifyId": cached_id,
            "source": cached.get("source") or "cache",
            "title": cached.get("title") or title,
            "artist": cached.get("artist") or artist,
        }
    titles = variants(title)
    artists = variants(artist)
    # Names hiding in the title, e.g. "삐그덕 (Feat. 아이유)".
    for inner in re.findall(r"\(([^)]*)\)", title or ""):
        if re.match(r"\s*(feat|ft|featuring|with)\b", inner, re.I):
            artists.extend(variants(inner))
    if not titles:
        return None
    exact = []
    for row in index:
        title_hit = any(name and name in row["title_norms"] for name in titles)
        artist_hit = any(
            artist_ok(name, other)
            for name in artists
            for other in row["artist_norms"]
        )
        if title_hit and artist_hit:
            exact.append(row)
    return choose(exact)


def remember(cache: dict, title: str, artist: str, spotify_id: str, source: str) -> None:
    cache[cache_key(title, artist)] = {
        "spotifyId": spotify_id,
        "title": title,
        "artist": artist,
        "source": source,
    }


def attach_spotify(song: dict, index: list[dict], cache: dict) -> str | None:
    hit = match_track(song.get("title", ""), song.get("artist", ""), index, cache)
    if hit and hit.get("spotifyId"):
        song["spotifyId"] = hit["spotifyId"]
        remember(
            cache,
            song.get("title", ""),
            song.get("artist", ""),
            hit["spotifyId"],
            hit.get("source") or "matched",
        )
        return hit["spotifyId"]
    song.pop("spotifyId", None)
    return None


def write_json(path: Path, payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def build_reels(existing: list, index: list[dict], cache: dict) -> tuple[list, int]:
    kept = []
    matched = 0
    for song in existing:
        item = {
            "rank": song["rank"],
            "id": song["id"],
            "title": song["title"],
            "artist": song["artist"],
        }
        if song.get("note"):
            item["note"] = song["note"]
        if attach_spotify(item, index, cache):
            matched += 1
        kept.append(item)
    return kept, matched


def main() -> int:
    try:
        yt_html = fetch(YT_URL)
        chart_date, chart_rows = parse_youtube_daily(yt_html)
    except Exception as exc:
        print(f"chart update skipped: {exc}", file=sys.stderr)
        return 1
    if len(chart_rows) < MIN_PARTIAL:
        print(
            f"note: kworb daily chart published {len(chart_rows)} songs; using that full table",
            file=sys.stderr,
        )

    try:
        existing = load_json(SONGS_PATH, {})
    except json.JSONDecodeError as exc:
        print(f"chart update skipped: songs.json: {exc}", file=sys.stderr)
        return 1
    reels = existing.get("ig")
    if not isinstance(reels, list) or not reels:
        print("chart update skipped: Reels list missing", file=sys.stderr)
        return 1

    index: list[dict] = []
    spotify_errors = []
    for source, url in SPOTIFY_URLS:
        try:
            index.extend(parse_spotify(fetch(url), source))
        except Exception as exc:
            spotify_errors.append(f"{source}: {exc}")
    if spotify_errors:
        print("spotify fetch warning: " + "; ".join(spotify_errors), file=sys.stderr)
    if not index and not CACHE_PATH.exists():
        print("chart update skipped: no Spotify charts and no cache", file=sys.stderr)
        return 1

    cache_doc = load_json(CACHE_PATH, {})
    cache = cache_doc.get("entries") if isinstance(cache_doc, dict) else None
    if not isinstance(cache, dict):
        cache = {}

    now = datetime.now(KST)
    yt_songs = []
    yt_matched = 0
    for row in chart_rows:
        item = {
            "rank": row["rank"],
            "id": f"yt-{row['rank']}",
            "title": row["title"],
            "artist": row["artist"],
        }
        if row.get("youtubeId"):
            item["youtubeId"] = row["youtubeId"]
        if attach_spotify(item, index, cache):
            yt_matched += 1
        yt_songs.append(item)

    ig_songs, ig_matched = build_reels(reels, index, cache)
    payload = {
        "collected": now.date().isoformat(),
        "updatedAt": now.isoformat(timespec="seconds"),
        "chartDate": chart_date,
        "yt": yt_songs,
        "ig": ig_songs,
    }
    cache_out = {
        "updatedAt": payload["updatedAt"],
        "entries": cache,
    }
    write_json(SONGS_PATH, payload)
    write_json(CACHE_PATH, cache_out)
    top = yt_songs[0]
    print(
        f"chartDate={chart_date} yt={len(yt_songs)} yt_spotify={yt_matched} "
        f"ig={len(ig_songs)} ig_spotify={ig_matched} "
        f"top={top['artist']} - {top['title']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
