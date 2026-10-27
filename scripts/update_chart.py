#!/usr/bin/env python3
"""Refresh the YouTube Korea weekly top 50 and Spotify track ids.

Fetches https://kworb.net/youtube/insights/kr.html and writes songs.json.
The Instagram Reels list is kept; only Spotify ids are added to those rows.
Spotify ids come from kworb country charts and artist pages (no Spotify API).
Mappings are cached in spotify-cache.json.

Fewer than 30 chart rows, a failed fetch, or a broken table exits without
writing. The file is also left untouched when the weekly chart and Spotify
ids are already current, so the daily job commits only when something changed.
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

YT_URL = "https://kworb.net/youtube/insights/kr.html"
SPOTIFY_URLS = (
    ("kr_weekly", "https://kworb.net/spotify/country/kr_weekly.html"),
    ("kr_daily", "https://kworb.net/spotify/country/kr_daily.html"),
    ("global_weekly", "https://kworb.net/spotify/country/global_weekly.html"),
    ("global_daily", "https://kworb.net/spotify/country/global_daily.html"),
    ("kr_weekly_totals", "https://kworb.net/spotify/country/kr_weekly_totals.html"),
    ("kr_daily_totals", "https://kworb.net/spotify/country/kr_daily_totals.html"),
    ("global_weekly_totals", "https://kworb.net/spotify/country/global_weekly_totals.html"),
    ("global_daily_totals", "https://kworb.net/spotify/country/global_daily_totals.html"),
)
SOURCE_RANK = {
    "kr_weekly": 0,
    "kr_daily": 1,
    "global_weekly": 2,
    "global_daily": 3,
    "kr_weekly_totals": 4,
    "kr_daily_totals": 5,
    "global_weekly_totals": 6,
    "global_daily_totals": 7,
    "artist": 8,
}
SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
MIN_ROWS = 30
TOP_N = 50
MAX_ARTIST_PAGES = 40

ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
TRACK_RE = re.compile(
    r'href="(?:\.\./|/spotify/)?track/([A-Za-z0-9]{22})\.html"[^>]*>(.*?)</a>',
    re.I | re.S,
)
ARTIST_RE = re.compile(
    r'href="(?:\.\./|/spotify/)?artist/([A-Za-z0-9]{22})\.html"[^>]*>(.*?)</a>',
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
    text = re.sub(r"\b(feat|ft|featuring)\b\.?", " ", text)
    text = re.sub(
        r"\b(live|lyric|lyrics|remix|version|ver|ost|official|audio|mv|inst|instrumental)\b",
        " ",
        text,
    )
    text = text.replace("라이브", " ").replace("가사", " ")
    text = text.replace("'", "")
    text = re.sub(r"[^0-9a-z가-힣\u3040-\u30ff\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _add(found: list[str], text: str) -> None:
    text = (text or "").strip()
    if text and text not in found:
        found.append(text)
    if not text:
        return
    hangul = " ".join(re.findall(r"[가-힣]+", text))
    latin = " ".join(re.findall(r"[a-z0-9]+", text))
    # A mixed Korean/English title matches on either script.
    if hangul and latin:
        _add_plain(found, hangul)
        _add_plain(found, latin)


def _add_plain(found: list[str], text: str) -> None:
    if text and text not in found:
        found.append(text)


def variants(value: str) -> list[str]:
    """Normalized forms. Parentheses and the other script are kept as aliases."""
    raw = unicodedata.normalize("NFKC", value or "")
    inners = [a or b for a, b in re.findall(r"\(([^)]*)\)|\[([^\]]*)\]", raw)]
    outer = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", raw)
    pieces = [outer]
    for inner in inners:
        if re.match(r"\s*(feat|ft|featuring)\b", inner, re.I):
            continue
        pieces.append(inner)
    found: list[str] = []
    for piece in pieces:
        _add(found, norm(piece))
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


def parse_youtube_weekly(html: str) -> tuple[str, list[dict]]:
    week_m = re.search(r"Week ending\s+(\d{4})/(\d{2})/(\d{2})", html)
    if not week_m:
        raise ChartError("chart week not found")
    chart_week = f"{week_m.group(1)}-{week_m.group(2)}-{week_m.group(3)}"
    table_m = re.search(
        r'<table[^>]*id="weeklytable"[^>]*>(.*?)</table>',
        html,
        re.I | re.S,
    )
    if not table_m:
        raise ChartError("weekly table not found")
    body_m = re.search(r"<tbody>(.*)</tbody>", table_m.group(1), re.I | re.S)
    if not body_m:
        raise ChartError("weekly table body not found")
    rows = []
    for row_html in ROW_RE.findall(body_m.group(1)):
        cells = CELL_RE.findall(row_html)
        if not cells:
            continue
        rank_m = re.search(r"\d+", strip_tags(cells[0]))
        if not rank_m:
            continue
        label = ""
        for cell in cells:
            text = strip_tags(cell)
            if " - " in text:
                label = text
                break
        if not label and len(cells) >= 3:
            label = strip_tags(cells[2])
        artist, title = split_artist_title(label)
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
    if len(rows) < MIN_ROWS:
        raise ChartError(f"only {len(rows)} songs")
    return chart_week, rows[:TOP_N]


def parse_spotify(html: str, source: str, artist_index: dict[str, str] | None = None) -> list[dict]:
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
        artist_ids = []
        for artist_id, artist_html in ARTIST_RE.findall(row_html):
            name = strip_tags(artist_html)
            if name and name not in artists:
                artists.append(name)
            if artist_id not in artist_ids:
                artist_ids.append(artist_id)
            if artist_index is not None and name:
                for key in variants(name):
                    artist_index.setdefault(key, artist_id)
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
                "artist_ids": artist_ids,
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


def song_artists(title: str, artist: str) -> list[str]:
    names = variants(artist)
    for inner in re.findall(r"\(([^)]*)\)", title or ""):
        if re.match(r"\s*(feat|ft|featuring)\b", inner, re.I):
            names.extend(variants(inner))
    return names


def match_track(title: str, artist: str, by_title: dict[str, list[dict]], cache: dict) -> dict | None:
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
    artists = song_artists(title, artist)
    if not titles:
        return None
    exact = []
    seen: set[str] = set()
    for name in titles:
        for row in by_title.get(name, []):
            if row["spotifyId"] in seen:
                continue
            if any(artist_ok(left, other) for left in artists for other in row["artist_norms"]):
                seen.add(row["spotifyId"])
                exact.append(row)
    return choose(exact)


def title_score(wanted: list[str], have: list[str]) -> int:
    score = 0
    for left in wanted:
        for right in have:
            if not left or not right:
                continue
            if left == right:
                score = max(score, 100)
            elif len(left) >= 5 and len(right) >= 5 and (left in right or right in left):
                score = max(score, 80)
    return score


def match_on_artist_page(
    title: str,
    artist: str,
    artist_index: dict[str, str],
    artist_pages: dict[str, list[dict]],
) -> dict | None:
    ids: list[str] = []
    for name in song_artists(title, artist):
        artist_id = artist_index.get(name)
        if artist_id and artist_id not in ids:
            ids.append(artist_id)
    if not ids or len(artist_pages) >= MAX_ARTIST_PAGES and not any(i in artist_pages for i in ids):
        # Still allow ids we already fetched. New fetches stop at the cap.
        ids = [artist_id for artist_id in ids if artist_id in artist_pages]
    titles = variants(title)
    best: list[tuple[int, dict]] = []
    for artist_id in ids[:3]:
        if artist_id not in artist_pages:
            if len(artist_pages) >= MAX_ARTIST_PAGES:
                continue
            try:
                html = fetch(f"https://kworb.net/spotify/artist/{artist_id}.html")
                artist_pages[artist_id] = parse_spotify(html, "artist")
            except Exception as exc:
                print(f"artist page skipped: {artist_id}: {exc}", file=sys.stderr)
                artist_pages[artist_id] = []
        for row in artist_pages.get(artist_id, []):
            score = title_score(titles, row["title_norms"])
            if score:
                best.append((score, row))
    if not best:
        return None
    best.sort(key=lambda item: (-item[0], SOURCE_RANK.get(item[1]["source"], 9)))
    top_score = best[0][0]
    winners = []
    for score, row in best:
        if score != top_score:
            break
        if row["spotifyId"] not in {item["spotifyId"] for item in winners}:
            winners.append(row)
    if len(winners) == 1 and top_score >= 80:
        return winners[0]
    return None


def remember(cache: dict, title: str, artist: str, spotify_id: str, source: str) -> None:
    cache[cache_key(title, artist)] = {
        "spotifyId": spotify_id,
        "title": title,
        "artist": artist,
        "source": source,
    }


def attach_spotify(
    song: dict,
    by_title: dict[str, list[dict]],
    cache: dict,
    artist_index: dict[str, str],
    artist_pages: dict[str, list[dict]],
) -> str | None:
    hit = match_track(song.get("title", ""), song.get("artist", ""), by_title, cache)
    if not hit:
        hit = match_on_artist_page(
            song.get("title", ""),
            song.get("artist", ""),
            artist_index,
            artist_pages,
        )
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


def build_reels(
    existing: list,
    by_title: dict[str, list[dict]],
    cache: dict,
    artist_index: dict[str, str],
    artist_pages: dict[str, list[dict]],
) -> tuple[list, int]:
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
        if attach_spotify(item, by_title, cache, artist_index, artist_pages):
            matched += 1
        kept.append(item)
    return kept, matched


def content_signature(doc: dict) -> tuple:
    yt = tuple(
        (row.get("rank"), row.get("title"), row.get("artist"), row.get("spotifyId"))
        for row in doc.get("yt") or []
    )
    ig = tuple(
        (
            row.get("rank"),
            row.get("id"),
            row.get("title"),
            row.get("artist"),
            row.get("note"),
            row.get("spotifyId"),
        )
        for row in doc.get("ig") or []
    )
    return (doc.get("chartWeek"), yt, ig)


def main() -> int:
    try:
        yt_html = fetch(YT_URL)
        chart_week, chart_rows = parse_youtube_weekly(yt_html)
    except Exception as exc:
        print(f"chart update skipped: {exc}", file=sys.stderr)
        return 1
    if len(chart_rows) < MIN_ROWS:
        print(f"chart update skipped: only {len(chart_rows)} songs", file=sys.stderr)
        return 1

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
    artist_index: dict[str, str] = {}
    spotify_errors = []
    for source, url in SPOTIFY_URLS:
        try:
            index.extend(parse_spotify(fetch(url), source, artist_index))
        except Exception as exc:
            spotify_errors.append(f"{source}: {exc}")
    if spotify_errors:
        print("spotify fetch warning: " + "; ".join(spotify_errors), file=sys.stderr)
    if not index and not CACHE_PATH.exists():
        print("chart update skipped: no Spotify charts and no cache", file=sys.stderr)
        return 1

    by_title: dict[str, list[dict]] = {}
    for row in index:
        for name in row["title_norms"]:
            by_title.setdefault(name, []).append(row)

    cache_doc = load_json(CACHE_PATH, {})
    cache = cache_doc.get("entries") if isinstance(cache_doc, dict) else None
    if not isinstance(cache, dict):
        cache = {}
    cache_before = json.dumps(cache, ensure_ascii=False, sort_keys=True)

    artist_pages: dict[str, list[dict]] = {}
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
        if attach_spotify(item, by_title, cache, artist_index, artist_pages):
            yt_matched += 1
        yt_songs.append(item)

    ig_songs, ig_matched = build_reels(reels, by_title, cache, artist_index, artist_pages)
    payload = {
        "collected": existing.get("collected") or now.date().isoformat(),
        "updatedAt": existing.get("updatedAt") or now.isoformat(timespec="seconds"),
        "chartDate": chart_week,
        "chartWeek": chart_week,
        "yt": yt_songs,
        "ig": ig_songs,
    }
    changed = content_signature(payload) != content_signature(existing)
    cache_changed = json.dumps(cache, ensure_ascii=False, sort_keys=True) != cache_before
    top = yt_songs[0]
    print(
        f"chartWeek={chart_week} yt={len(yt_songs)} yt_spotify={yt_matched} "
        f"ig={len(ig_songs)} ig_spotify={ig_matched} "
        f"top={top['artist']} - {top['title']} "
        f"changed={str(changed).lower()}"
    )
    if not changed and not cache_changed:
        print("songs.json unchanged")
        return 0
    if changed:
        payload["collected"] = now.date().isoformat()
        payload["updatedAt"] = now.isoformat(timespec="seconds")
        write_json(SONGS_PATH, payload)
    if cache_changed:
        write_json(
            CACHE_PATH,
            {"updatedAt": now.isoformat(timespec="seconds"), "entries": cache},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
