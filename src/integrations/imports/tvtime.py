"""Native TV Time GDPR ZIP importer for FlexiHub.

The importer reads only the TV Time CSV files required for watch history,
converts TVDB identifiers/numbering to TMDB, builds a FlexiHub CSV in memory,
and delegates persistence to the existing FlexiHub/Yamtrack CSV importer.

Sensitive GDPR files inside the archive are never opened.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from django.conf import settings

from integrations.imports import yamtrack
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_REQUIRED_FILE_BYTES = 40 * 1024 * 1024

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
IMG_NONE = (
    "https://www.themoviedb.org/assets/2/v4/glyphicons/basic/"
    "glyphicons-basic-38-picture-grey-c2ebdbb057f2a7614185931650f8cee23fa137b93812ccb132b9df511df1cfac.svg"
)

WHITELIST = {
    "tracking-prod-records-v2.csv",
    "tracking-prod-records.csv",
    "followed_tv_show.csv",
    "user_tv_show_data.csv",
}

REQUIRED_FILES = {
    "tracking-prod-records-v2.csv",
    "tracking-prod-records.csv",
}

CSV_FIELDS = [
    "media_id",
    "source",
    "media_type",
    "title",
    "image",
    "season_number",
    "episode_number",
    "score",
    "progress",
    "status",
    "start_date",
    "end_date",
    "notes",
    "created_at",
    "progressed_at",
]


def _validate_archive(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise MediaImportError(
            "TV Time ZIP contains too many files and was rejected for safety."
        )

    total_uncompressed = sum(max(info.file_size, 0) for info in members)
    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise MediaImportError(
            "TV Time ZIP is too large after decompression and was rejected for safety."
        )

    available = {info.filename for info in members}
    missing = REQUIRED_FILES - available
    if missing:
        raise MediaImportError(
            "This file does not look like a supported TV Time GDPR export. "
            f"Missing: {', '.join(sorted(missing))}."
        )

    for info in members:
        if info.filename in WHITELIST and info.file_size > MAX_REQUIRED_FILE_BYTES:
            raise MediaImportError(
                f"TV Time file {info.filename} is unexpectedly large."
            )


def _read_uploaded_zip(uploaded_file: Any) -> bytes:
    if isinstance(uploaded_file, bytes):
        data = uploaded_file
    elif isinstance(uploaded_file, bytearray):
        data = bytes(uploaded_file)
    elif hasattr(uploaded_file, "read"):
        try:
            uploaded_file.seek(0)
        except (AttributeError, OSError):
            pass
        data = uploaded_file.read()
    else:
        raise MediaImportError("TV Time ZIP file is required.")

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise MediaImportError("Invalid TV Time ZIP upload.")
    if not data:
        raise MediaImportError("TV Time ZIP file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaImportError("TV Time ZIP is larger than the allowed upload size.")
    return data


def _build_flexihub_csv(rows: list[dict[str, Any]]) -> io.BytesIO:
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    payload = io.BytesIO(text.getvalue().encode("utf-8"))
    payload.name = "tvtime-flexihub-import.csv"
    payload.seek(0)
    return payload


def _conversion_warnings(report: dict[str, Any]) -> list[str]:
    warnings = []
    for warning in report.get("warnings", []):
        # Cross-series TVDB mismatches that were safely recovered through
        # validated TV Time numbering are informational, not failed imports.
        if "using TV Time numbering" in warning and "as fallback" in warning:
            continue
        warnings.append(warning)
    return warnings

def normalize_text(value: str | None) -> str:
    value = value or ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().casefold()
    return re.sub(r"\s+", " ", value)


def parse_dt(value: str | None) -> str:
    """Return an ISO local datetime string accepted by Django forms."""
    value = (value or "").strip()
    if not value:
        return ""
    value = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.replace(microsecond=0).isoformat()
    except ValueError:
        # TV Time usually exports `YYYY-MM-DD HH:MM:SS`.
        return value.replace(" ", "T", 1)


def release_year(value: str | None) -> int | None:
    value = (value or "").strip()
    match = re.match(r"^(\d{4})-", value)
    if not match:
        return None
    year = int(match.group(1))
    if year < 1900 or year > 2100:
        return None
    return year


def image_url(path: str | None, fallback: str = IMG_NONE) -> str:
    if not path:
        return fallback
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{TMDB_IMAGE_BASE}{path}"


def read_csv_from_zip(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in WHITELIST:
        raise ValueError(f"Refusing to read non-whitelisted file: {name}")
    try:
        raw = archive.read(name)
    except KeyError:
        return []
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


@dataclass(frozen=True)
class EpisodeEvent:
    tvdb_series_id: str
    series_name: str
    # Normalized numbering exported by TV Time. This is retained for fallback
    # when an episode cannot be resolved directly by its TVDB episode ID.
    season_number: int
    episode_number: int
    # TV Time also exports the original TVDB-style numbering in s_no/ep_no.
    # Older shows can have rows where these fields disagree with the normalized
    # season_number/episode_number pair. Keeping both lets us reconstruct
    # completion without corrupting the actual TMDB episode mapping.
    original_season_number: int
    original_episode_number: int
    bulk_type: str
    tvdb_episode_id: str
    watched_at: str
    is_rewatch: bool


@dataclass(frozen=True)
class MovieEvent:
    title: str
    release_year: int | None
    watched_at: str
    uuid: str
    is_rewatch: bool


@dataclass
class ParsedArchive:
    episode_events: list[EpisodeEvent]
    followed_series: dict[str, dict[str, str]]
    movie_events: list[MovieEvent]
    skipped_episode_zero: int
    exact_duplicate_events_removed: int


class TmdbClient:
    def __init__(self, api_key: str, cache_path: Path | None = None):
        self.api_key = api_key.strip()
        self.cache_path = cache_path
        self.cache: dict[str, Any] = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.cache = {}

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        params["api_key"] = self.api_key
        url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"
        cache_key = url.replace(self.api_key, "<API_KEY>")
        if cache_key in self.cache:
            return self.cache[cache_key]

        for attempt in range(6):
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "FlexiHub-TVTime-Converter/1.0",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    self.cache[cache_key] = data
                    if len(self.cache) % 25 == 0:
                        self.save_cache()
                    time.sleep(0.03)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry = int(exc.headers.get("Retry-After", "2"))
                    time.sleep(retry + 1)
                    continue
                if 500 <= exc.code < 600 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise RuntimeError(
                    f"TMDB HTTP {exc.code} for {path}: {body[:300]}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"TMDB connection error for {path}: {exc}") from exc

        raise RuntimeError(f"TMDB request failed after retries: {path}")

    def find_tv_by_tvdb(self, tvdb_id: str) -> dict[str, Any] | None:
        data = self._request(
            f"/find/{urllib.parse.quote(str(tvdb_id))}",
            {"external_source": "tvdb_id", "language": "pt-BR"},
        )
        results = data.get("tv_results") or []
        return results[0] if results else None

    def find_episode_by_tvdb(self, tvdb_episode_id: str) -> dict[str, Any] | None:
        """Resolve a TVDB episode ID directly to its TMDB episode record."""
        tvdb_episode_id = str(tvdb_episode_id or "").strip()
        if not tvdb_episode_id:
            return None
        data = self._request(
            f"/find/{urllib.parse.quote(tvdb_episode_id)}",
            {"external_source": "tvdb_id", "language": "pt-BR"},
        )
        results = data.get("tv_episode_results") or []
        return results[0] if results else None

    def tv_detail(self, tmdb_id: str | int) -> dict[str, Any]:
        return self._request(f"/tv/{tmdb_id}", {"language": "pt-BR"})

    def season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, Any]:
        return self._request(
            f"/tv/{tmdb_id}/season/{season_number}",
            {"language": "pt-BR"},
        )

    def search_movie(self, title: str, year: int | None) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "query": title,
            "language": "pt-BR",
            "include_adult": "false",
        }
        if year:
            params["primary_release_year"] = year
        data = self._request("/search/movie", params)
        candidates = data.get("results") or []

        # If a year-filtered search came back empty, retry without the year.
        if not candidates and year:
            data = self._request(
                "/search/movie",
                {
                    "query": title,
                    "language": "pt-BR",
                    "include_adult": "false",
                },
            )
            candidates = data.get("results") or []

        if not candidates:
            return None

        target = normalize_text(title)
        best: tuple[float, dict[str, Any]] | None = None
        for candidate in candidates[:20]:
            c_titles = [
                normalized
                for normalized in (
                    normalize_text(candidate.get("title")),
                    normalize_text(candidate.get("original_title")),
                )
                if normalized
            ]

            # TMDB can occasionally return an incomplete search result without
            # either title field. Ignore that candidate instead of crashing on
            # max() with an empty iterable.
            if not c_titles:
                continue

            similarity = max(
                SequenceMatcher(None, target, c_title).ratio()
                for c_title in c_titles
            )
            score = similarity * 100
            c_year = release_year(candidate.get("release_date"))
            if year and c_year == year:
                score += 30
            if target in c_titles:
                score += 50
            if best is None or score > best[0]:
                best = (score, candidate)

        if not best:
            return None
        # Conservative threshold: title must at least be reasonably similar.
        if best[0] < 72:
            return None
        return best[1]


def parse_archive(zip_bytes: bytes) -> ParsedArchive:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        _validate_archive(archive)
        v2 = read_csv_from_zip(archive, "tracking-prod-records-v2.csv")
        old = read_csv_from_zip(archive, "tracking-prod-records.csv")
        followed = read_csv_from_zip(archive, "followed_tv_show.csv")
        user_tv = read_csv_from_zip(archive, "user_tv_show_data.csv")

    # Build a fallback TVDB series-id/name map.
    id_to_name: dict[str, str] = {}
    for row in followed + user_tv:
        sid = (row.get("tv_show_id") or "").strip()
        name = (row.get("tv_show_name") or "").strip()
        if sid and name:
            id_to_name.setdefault(sid, name)
    for row in old:
        sid = (row.get("series_id") or "").strip()
        name = (row.get("series_name") or "").strip()
        if sid and name:
            id_to_name.setdefault(sid, name)

    raw_events: list[EpisodeEvent] = []
    skipped_episode_zero = 0
    for row in v2:
        key = row.get("key") or ""
        if not key.startswith(("watch-episode", "rewatch-episode")):
            continue
        sid = (row.get("s_id") or "").strip()
        season_raw = (row.get("season_number") or row.get("s_no") or "").strip()
        episode_raw = (row.get("episode_number") or row.get("ep_no") or "").strip()
        original_season_raw = (row.get("s_no") or season_raw).strip()
        original_episode_raw = (row.get("ep_no") or episode_raw).strip()
        if not sid or not season_raw.isdigit() or not episode_raw.isdigit():
            continue
        season_number = int(season_raw)
        episode_number = int(episode_raw)
        original_season_number = (
            int(original_season_raw)
            if original_season_raw.isdigit()
            else season_number
        )
        original_episode_number = (
            int(original_episode_raw)
            if original_episode_raw.isdigit()
            else episode_number
        )
        if episode_number <= 0:
            skipped_episode_zero += 1
            continue
        raw_events.append(
            EpisodeEvent(
                tvdb_series_id=sid,
                series_name=(row.get("series_name") or id_to_name.get(sid, "")).strip(),
                season_number=season_number,
                episode_number=episode_number,
                original_season_number=original_season_number,
                original_episode_number=original_episode_number,
                bulk_type=(row.get("bulk_type") or "").strip(),
                tvdb_episode_id=(row.get("episode_id") or row.get("ep_id") or "").strip(),
                watched_at=parse_dt(row.get("created_at") or row.get("updated_at")),
                is_rewatch=key.startswith("rewatch-episode"),
            )
        )

    # Remove only exact duplicate export records. Distinct watch timestamps for the
    # same episode are retained as legitimate rewatches/multiple watches.
    seen_exact: set[tuple[Any, ...]] = set()
    episode_events: list[EpisodeEvent] = []
    exact_duplicate_events_removed = 0
    for event in sorted(
        raw_events,
        key=lambda x: (
            x.watched_at,
            x.tvdb_series_id,
            x.season_number,
            x.episode_number,
            x.is_rewatch,
        ),
    ):
        exact_key = (
            event.tvdb_series_id,
            event.season_number,
            event.episode_number,
            event.watched_at,
            event.is_rewatch,
        )
        if exact_key in seen_exact:
            exact_duplicate_events_removed += 1
            continue
        seen_exact.add(exact_key)
        episode_events.append(event)

    followed_series: dict[str, dict[str, str]] = {}
    for row in followed:
        sid = (row.get("tv_show_id") or "").strip()
        if not sid:
            continue
        followed_series[sid] = {
            "name": (row.get("tv_show_name") or id_to_name.get(sid, "")).strip(),
            "followed_at": parse_dt(row.get("created_at")),
            "archived": (row.get("archived") or "0").strip(),
        }

    movie_events: list[MovieEvent] = []
    for row in old:
        if row.get("type") not in {"watch", "rewatch"}:
            continue
        if row.get("entity_type") != "movie":
            continue
        title = (row.get("movie_name") or "").strip()
        if not title:
            continue
        movie_events.append(
            MovieEvent(
                title=title,
                release_year=release_year(row.get("release_date")),
                watched_at=parse_dt(row.get("created_at") or row.get("updated_at")),
                uuid=(row.get("uuid") or "").strip(),
                is_rewatch=row.get("type") == "rewatch",
            )
        )

    movie_events.sort(key=lambda x: x.watched_at)
    return ParsedArchive(
        episode_events=episode_events,
        followed_series=followed_series,
        movie_events=movie_events,
        skipped_episode_zero=skipped_episode_zero,
        exact_duplicate_events_removed=exact_duplicate_events_removed,
    )


def audit(parsed: ParsedArchive) -> dict[str, Any]:
    unique_eps = {
        (e.tvdb_series_id, e.season_number, e.episode_number)
        for e in parsed.episode_events
    }
    watched_series = {e.tvdb_series_id for e in parsed.episode_events}
    rewatch_events = sum(1 for e in parsed.episode_events if e.is_rewatch)
    distinct_movie_ids = {e.uuid for e in parsed.movie_events if e.uuid}
    return {
        "episode_watch_events": len(parsed.episode_events),
        "unique_episodes": len(unique_eps),
        "series_with_watch_history": len(watched_series),
        "followed_series": len(parsed.followed_series),
        "followed_series_without_watch_history": len(
            set(parsed.followed_series) - watched_series
        ),
        "episode_rewatch_events": rewatch_events,
        "movie_watch_events": len(parsed.movie_events),
        "unique_movies_by_tvtime_uuid": len(distinct_movie_ids),
        "exact_duplicate_episode_events_removed": parsed.exact_duplicate_events_removed,
        "invalid_episode_zero_records_skipped": parsed.skipped_episode_zero,
    }



def row_template(**values: Any) -> dict[str, Any]:
    row = {field: "" for field in CSV_FIELDS}
    row.update(values)
    return row


def convert(parsed: ParsedArchive, tmdb: TmdbClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    warnings: list[str] = []
    rows_tv: list[dict[str, Any]] = []
    rows_seasons: list[dict[str, Any]] = []
    rows_episodes: list[dict[str, Any]] = []
    rows_movies: list[dict[str, Any]] = []
    series_status_details: list[dict[str, Any]] = []
    season_status_details: list[dict[str, Any]] = []

    events_by_series: dict[str, list[EpisodeEvent]] = defaultdict(list)
    for event in parsed.episode_events:
        events_by_series[event.tvdb_series_id].append(event)

    all_series_ids = sorted(set(events_by_series) | set(parsed.followed_series), key=int)
    series_map: dict[str, dict[str, Any]] = {}
    season_cache: dict[tuple[int, int], dict[str, Any]] = {}

    # Resolve the TV series. Prefer an exact TVDB episode -> TMDB episode mapping
    # whenever watch history exists. This is more reliable than trusting the
    # TVDB season/episode numbering because numbering can differ between TVDB
    # and TMDB (especially specials, split/combined episodes, and older shows).
    logger.info(f"Resolving {len(all_series_ids)} TV series through TMDB...")
    for index, tvdb_id in enumerate(all_series_ids, 1):
        found_series: dict[str, Any] | None = None
        mapped_tmdb_id: int | None = None
        mapped_from_episode = False

        for event in events_by_series.get(tvdb_id, []):
            if not event.tvdb_episode_id:
                continue
            episode_match = tmdb.find_episode_by_tvdb(event.tvdb_episode_id)
            if not episode_match:
                continue
            show_id = episode_match.get("show_id")
            if show_id is None:
                continue
            try:
                mapped_tmdb_id = int(show_id)
            except (TypeError, ValueError):
                continue
            mapped_from_episode = True
            break

        if mapped_tmdb_id is None:
            found_series = tmdb.find_tv_by_tvdb(tvdb_id)
            if found_series:
                try:
                    mapped_tmdb_id = int(found_series["id"])
                except (KeyError, TypeError, ValueError):
                    mapped_tmdb_id = None

        if mapped_tmdb_id is None:
            fallback_name = parsed.followed_series.get(tvdb_id, {}).get("name", "")
            if not fallback_name and events_by_series.get(tvdb_id):
                fallback_name = events_by_series[tvdb_id][0].series_name
            warnings.append(
                f"Series TVDB {tvdb_id} ({fallback_name or 'unknown title'}): not found in TMDB."
            )
            logger.info(
                f"  [{index}/{len(all_series_ids)}] NOT FOUND TVDB "
                f"{tvdb_id} {fallback_name}"
            )
            continue

        detail = tmdb.tv_detail(mapped_tmdb_id)
        series_map[tvdb_id] = {
            "tmdb_id": mapped_tmdb_id,
            "detail": detail,
            "image": image_url(
                detail.get("poster_path")
                or (found_series or {}).get("poster_path")
            ),
            "title": (
                detail.get("name")
                or (found_series or {}).get("name")
                or ""
            ),
            "mapped_from_episode": mapped_from_episode,
        }
        mapping_label = "episode ID" if mapped_from_episode else "series ID"
        logger.info(
            f"  [{index}/{len(all_series_ids)}] {tvdb_id} -> "
            f"TMDB {mapped_tmdb_id}: {series_map[tvdb_id]['title']} "
            f"({mapping_label})"
        )

    # Resolve every watched episode by its TVDB episode ID. The TV Time export
    # contains episode_id/ep_id values, and TMDB's /find endpoint accepts TVDB
    # episode IDs. This avoids silent wrong mappings when TVDB and TMDB use
    # different season/episode numbering.
    total_events = len(parsed.episode_events)
    logger.info(f"Resolving {total_events} episode watch events by TVDB episode ID...")
    resolved_events_by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exact_episode_matches = 0
    numbering_fallbacks = 0
    unresolved_episode_ids = 0
    cross_series_mismatches = 0

    for index, event in enumerate(parsed.episode_events, 1):
        mapping = series_map.get(event.tvdb_series_id)
        if not mapping:
            unresolved_episode_ids += 1
            if index == total_events or index % 50 == 0:
                logger.info(
                    f"  Episodes {index}/{total_events} | exact={exact_episode_matches} "
                    f"fallback={numbering_fallbacks} unresolved={unresolved_episode_ids}"
                )
            continue

        tmdb_id = int(mapping["tmdb_id"])
        resolved_season = event.season_number
        resolved_episode = event.episode_number
        exact_match = False
        episode_match: dict[str, Any] | None = None

        if event.tvdb_episode_id:
            episode_match = tmdb.find_episode_by_tvdb(event.tvdb_episode_id)

        if episode_match:
            try:
                match_show_id = int(episode_match.get("show_id"))
                match_season = int(episode_match.get("season_number"))
                match_episode = int(episode_match.get("episode_number"))
            except (TypeError, ValueError):
                match_show_id = -1
                match_season = -1
                match_episode = -1

            if (
                match_show_id == tmdb_id
                and match_season >= 0
                and match_episode > 0
            ):
                resolved_season = match_season
                resolved_episode = match_episode
                exact_match = True
                exact_episode_matches += 1
            elif match_show_id > 0 and match_show_id != tmdb_id:
                # TMDB occasionally associates a TVDB episode ID with a different
                # show even though TV Time's own series/season/episode coordinates
                # are internally consistent. Do not trust that external-ID match,
                # but also do not throw away the watch event. Fall back to the
                # original TV Time season/episode numbering inside the already
                # resolved parent series. The season payload is validated later,
                # so an invalid SxxExx still gets skipped safely.
                cross_series_mismatches += 1
                numbering_fallbacks += 1
                warnings.append(
                    f"{mapping['title']} TVDB episode {event.tvdb_episode_id}: "
                    f"TMDB points to series {match_show_id}, expected {tmdb_id}; "
                    f"using TV Time numbering S{event.season_number:02d}E{event.episode_number:02d} as fallback."
                )
            else:
                numbering_fallbacks += 1
        else:
            numbering_fallbacks += 1
            if event.tvdb_episode_id:
                unresolved_episode_ids += 1

        resolved_events_by_series[event.tvdb_series_id].append(
            {
                "event": event,
                "season_number": resolved_season,
                "episode_number": resolved_episode,
                "exact_match": exact_match,
                "episode_match": episode_match if exact_match else None,
            }
        )

        if index == total_events or index % 50 == 0:
            logger.info(
                f"  Episodes {index}/{total_events} | exact={exact_episode_matches} "
                f"fallback={numbering_fallbacks} unresolved={unresolved_episode_ids}"
            )

    today = dt.date.today()

    for tvdb_id in all_series_ids:
        mapping = series_map.get(tvdb_id)
        if not mapping:
            continue

        tmdb_id = int(mapping["tmdb_id"])
        detail = mapping["detail"]
        tv_image = mapping["image"]
        title = mapping["title"]
        resolved_events = resolved_events_by_series.get(tvdb_id, [])
        original_events = events_by_series.get(tvdb_id, [])

        first_event = min(
            (item["event"].watched_at for item in resolved_events if item["event"].watched_at),
            default="",
        )

        provider_status = (detail.get("status") or "").casefold()
        is_ended = provider_status in {"ended", "canceled", "cancelled"}

        # We decide the TV-level status only after processing all seasons.
        # This is important because TVDB and TMDB can disagree on individual
        # episode numbering even when a season is demonstrably complete.
        regular_season_numbers = {
            int(season.get("season_number") or 0)
            for season in (detail.get("seasons") or [])
            if int(season.get("season_number") or 0) > 0
            and int(season.get("episode_count") or 0) > 0
        }
        season_status_by_number: dict[int, str] = {}

        by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in resolved_events:
            by_season[int(item["season_number"])].append(item)

        # TV Time keeps two numbering systems in the GDPR export. The original
        # s_no/ep_no pair is especially useful for older shows where its newer
        # normalized season_number/episode_number fields were reshuffled. A
        # classic example is Rocket Power: the original numbering contains a
        # contiguous 1..44 for season 1 and 1..34 for season 2 even though some
        # normalized rows are placed in another season. We use this only as a
        # conservative completion hint; exact episode rows still come from TMDB.
        original_seen_by_season: dict[int, set[int]] = defaultdict(set)
        original_bulk_types_by_season: dict[int, set[str]] = defaultdict(set)
        for event in original_events:
            if event.original_season_number <= 0 or event.original_episode_number <= 0:
                continue
            original_seen_by_season[event.original_season_number].add(
                event.original_episode_number
            )
            if event.bulk_type:
                original_bulk_types_by_season[event.original_season_number].add(
                    event.bulk_type
                )

        for season_number in sorted(by_season):
            try:
                season_detail = tmdb.season_detail(tmdb_id, season_number)
            except RuntimeError as exc:
                warnings.append(
                    f"{title} season {season_number}: TMDB lookup failed: {exc}"
                )
                continue

            season_cache[(tmdb_id, season_number)] = season_detail
            episode_meta = {
                int(ep.get("episode_number") or 0): ep
                for ep in (season_detail.get("episodes") or [])
                if int(ep.get("episode_number") or 0) > 0
            }

            watched_unique = {
                int(item["episode_number"])
                for item in by_season[season_number]
            }
            valid_episode_numbers = set(episode_meta)
            missing_from_tmdb = sorted(watched_unique - valid_episode_numbers)
            for ep_no in missing_from_tmdb:
                warnings.append(
                    f"{title} S{season_number:02d}E{ep_no:02d}: "
                    "not found in TMDB after TVDB-ID resolution; skipped."
                )

            released_numbers: set[int] = set()
            future_exists = False
            for ep_no, ep in episode_meta.items():
                air_date = (ep.get("air_date") or "").strip()
                if not air_date:
                    future_exists = True
                    continue
                try:
                    air = dt.date.fromisoformat(air_date)
                except ValueError:
                    future_exists = True
                    continue
                if air <= today:
                    released_numbers.add(ep_no)
                else:
                    future_exists = True

            # The home page is built from Season.status, not from the parent
            # TV.status. Prefer exact resolved episode coverage, but for ended
            # legacy shows allow a second, narrowly-scoped signal from TV Time's
            # original s_no/ep_no numbering. That signal is accepted only when
            # the original watched set is perfectly contiguous from episode 1
            # and ends at exactly the same final released episode number that
            # TMDB reports for this season. This fixes numbering migrations
            # without treating a season with genuine gaps as completed.
            original_seen = original_seen_by_season.get(season_number, set())
            original_max = max(original_seen) if original_seen else 0
            original_contiguous = bool(original_seen) and original_seen == set(
                range(1, original_max + 1)
            )
            provider_released_max = max(released_numbers) if released_numbers else 0
            legacy_numbering_complete = (
                season_number > 0
                and is_ended
                and original_contiguous
                and provider_released_max > 0
                and original_max == provider_released_max
            )

            if season_number > 0 and is_ended:
                if released_numbers and released_numbers.issubset(watched_unique):
                    season_status = "Completed"
                    season_status_reason = "resolved_episode_coverage"
                elif legacy_numbering_complete:
                    season_status = "Completed"
                    season_status_reason = "tvtime_original_numbering_complete"
                elif (
                    not released_numbers
                    and valid_episode_numbers
                    and valid_episode_numbers.issubset(watched_unique)
                ):
                    season_status = "Completed"
                    season_status_reason = "provider_episodes_without_air_dates_covered"
                else:
                    season_status = "In progress"
                    season_status_reason = "released_episodes_incomplete"
            elif (
                released_numbers
                and released_numbers.issubset(watched_unique)
                and not future_exists
            ):
                season_status = "Completed"
                season_status_reason = "all_released_episodes_covered_no_future"
            else:
                season_status = "In progress"
                season_status_reason = "season_not_complete"

            season_status_by_number[season_number] = season_status
            season_status_details.append(
                {
                    "title": title,
                    "tmdb_id": tmdb_id,
                    "season_number": season_number,
                    "status": season_status,
                    "reason": season_status_reason,
                    "resolved_watched_count": len(watched_unique & valid_episode_numbers),
                    "provider_released_count": len(released_numbers),
                    "provider_released_max": provider_released_max,
                    "tvtime_original_seen_count": len(original_seen),
                    "tvtime_original_max": original_max,
                    "tvtime_original_contiguous": original_contiguous,
                    "tvtime_bulk_types": sorted(
                        original_bulk_types_by_season.get(season_number, set())
                    ),
                }
            )

            season_image = image_url(season_detail.get("poster_path"), tv_image)
            season_title = season_detail.get("name") or f"Temporada {season_number}"
            season_first = min(
                (
                    item["event"].watched_at
                    for item in by_season[season_number]
                    if item["event"].watched_at
                ),
                default="",
            )
            rows_seasons.append(
                row_template(
                    media_id=tmdb_id,
                    source="tmdb",
                    media_type="season",
                    title=season_title,
                    image=season_image,
                    season_number=season_number,
                    progress=0,
                    status=season_status,
                    notes="Importado do TV Time",
                    progressed_at=season_first,
                )
            )

            seen_exact_output: set[tuple[int, int, str, bool]] = set()
            for item in sorted(
                by_season[season_number],
                key=lambda x: x["event"].watched_at,
            ):
                event: EpisodeEvent = item["event"]
                episode_number = int(item["episode_number"])
                ep = episode_meta.get(episode_number)

                # If TMDB /find resolved the episode exactly, use its metadata as
                # a safe fallback if the season payload omits the episode object.
                exact_ep = item.get("episode_match") or {}
                if not ep and exact_ep:
                    ep = exact_ep

                if not ep:
                    continue

                exact = (
                    season_number,
                    episode_number,
                    event.watched_at,
                    event.is_rewatch,
                )
                if exact in seen_exact_output:
                    continue
                seen_exact_output.add(exact)

                ep_title = ep.get("name") or title
                ep_image = image_url(ep.get("still_path"), season_image)
                rows_episodes.append(
                    row_template(
                        media_id=tmdb_id,
                        source="tmdb",
                        media_type="episode",
                        title=ep_title,
                        image=ep_image,
                        season_number=season_number,
                        episode_number=episode_number,
                        end_date=event.watched_at,
                        notes=(
                            "Reassistido no TV Time"
                            if event.is_rewatch
                            else "Importado do TV Time"
                        ),
                        progressed_at=event.watched_at,
                    )
                )

        # Final TV status. A finished TV show is Completed only when every
        # regular season is Completed. Do not use a raw total-episode count as
        # a fallback: numbering differences can inflate that count and falsely
        # mark a show complete while one season still has episodes left.
        completed_regular_seasons = {
            season_number
            for season_number, status in season_status_by_number.items()
            if season_number > 0 and status == "Completed"
        }

        if not original_events:
            tv_status = "Planning"
            tv_status_reason = "followed_without_watch_history"
        elif (
            is_ended
            and regular_season_numbers
            and regular_season_numbers.issubset(completed_regular_seasons)
        ):
            tv_status = "Completed"
            tv_status_reason = "all_regular_seasons_completed"
        else:
            tv_status = "In progress"
            tv_status_reason = (
                "provider_ended_but_regular_seasons_incomplete"
                if is_ended
                else "provider_series_not_ended"
            )

        rows_tv.append(
            row_template(
                media_id=tmdb_id,
                source="tmdb",
                media_type="tv",
                title=title,
                image=tv_image,
                progress=0,
                status=tv_status,
                notes="Importado do TV Time",
                progressed_at=(
                    first_event
                    or parsed.followed_series.get(tvdb_id, {}).get("followed_at", "")
                ),
            )
        )

        series_status_details.append(
            {
                "title": title,
                "tvdb_id": tvdb_id,
                "tmdb_id": tmdb_id,
                "provider_status": detail.get("status") or "",
                "import_status": tv_status,
                "reason": tv_status_reason,
                "regular_seasons": sorted(regular_season_numbers),
                "completed_regular_seasons": sorted(completed_regular_seasons),
            }
        )

    status_counts = Counter(
        item["import_status"] for item in series_status_details
    )
    status_titles = {
        status: sorted(
            item["title"]
            for item in series_status_details
            if item["import_status"] == status
        )
        for status in ("Completed", "In progress", "Planning")
    }

    logger.info("Series status preview:")
    logger.info(f"  Completed:   {status_counts.get('Completed', 0)}")
    logger.info(f"  In progress: {status_counts.get('In progress', 0)}")
    logger.info(f"  Planning:    {status_counts.get('Planning', 0)}")

    logger.info(f"Resolving {len(parsed.movie_events)} movie watch events through TMDB...")
    movie_mapping_cache: dict[tuple[str, int | None], dict[str, Any] | None] = {}
    for index, event in enumerate(parsed.movie_events, 1):
        key = (normalize_text(event.title), event.release_year)
        if key not in movie_mapping_cache:
            movie_mapping_cache[key] = tmdb.search_movie(event.title, event.release_year)
        movie = movie_mapping_cache[key]
        if not movie:
            warnings.append(
                f"Movie {event.title}"
                + (f" ({event.release_year})" if event.release_year else "")
                + ": not confidently matched in TMDB."
            )
            logger.info(f"  [{index}/{len(parsed.movie_events)}] NOT FOUND: {event.title}")
            continue

        movie_id = int(movie["id"])
        movie_title = movie.get("title") or movie.get("original_title") or event.title
        rows_movies.append(
            row_template(
                media_id=movie_id,
                source="tmdb",
                media_type="movie",
                title=movie_title,
                image=image_url(movie.get("poster_path")),
                status="Completed",
                end_date=event.watched_at,
                notes=(
                    "Reassistido no TV Time"
                    if event.is_rewatch
                    else "Importado do TV Time"
                ),
                progressed_at=event.watched_at,
            )
        )
        logger.info(f"  [{index}/{len(parsed.movie_events)}] TMDB {movie_id}: {movie_title}")

    tmdb.save_cache()
    output_rows = rows_tv + rows_seasons + rows_episodes + rows_movies
    report = audit(parsed)
    report.update(
        {
            "resolved_series": len(rows_tv),
            "series_mapped_from_episode_id": sum(
                1 for mapping in series_map.values() if mapping.get("mapped_from_episode")
            ),
            "season_rows": len(rows_seasons),
            "episode_rows": len(rows_episodes),
            "movie_rows": len(rows_movies),
            "output_rows_total": len(output_rows),
            "episode_id_exact_matches": exact_episode_matches,
            "episode_numbering_fallbacks": numbering_fallbacks,
            "episode_id_unresolved": unresolved_episode_ids,
            "episode_cross_series_mismatches": cross_series_mismatches,
            "series_status_counts": {
                "Completed": status_counts.get("Completed", 0),
                "In progress": status_counts.get("In progress", 0),
                "Planning": status_counts.get("Planning", 0),
            },
            "series_status_titles": status_titles,
            "series_status_details": series_status_details,
            "season_status_details": season_status_details,
            "warnings_count": len(warnings),
            "warnings": warnings,
        }
    )
    return output_rows, report


def importer(file, user, mode):
    """Import a TV Time GDPR ZIP directly into FlexiHub."""
    zip_bytes = _read_uploaded_zip(file)

    try:
        parsed = parse_archive(zip_bytes)
    except zipfile.BadZipFile as exc:
        raise MediaImportError(
            "Invalid ZIP file. Upload the original TV Time GDPR export."
        ) from exc
    except MediaImportError:
        raise
    except (RuntimeError, ValueError, UnicodeError) as exc:
        raise MediaImportError(str(exc)) from exc

    api_key = str(getattr(settings, "TMDB_API", "") or "").strip()
    if not api_key:
        raise MediaImportError(
            "TMDB_API is not configured on the FlexiHub server."
        )

    tmdb = TmdbClient(api_key)
    try:
        rows, report = convert(parsed, tmdb)
    except MediaImportError:
        raise
    except Exception as exc:
        logger.exception("TV Time conversion failed for user %s", user.id)
        raise MediaImportError(
            "Could not convert the TV Time export. Please try again or review the import logs."
        ) from exc

    logger.info(
        "TV Time conversion for user %s produced %s rows: %s series, %s seasons, "
        "%s episodes, %s movies; %s conversion warnings.",
        user.id,
        report.get("output_rows_total", len(rows)),
        report.get("resolved_series", 0),
        report.get("season_rows", 0),
        report.get("episode_rows", 0),
        report.get("movie_rows", 0),
        report.get("warnings_count", 0),
    )

    csv_file = _build_flexihub_csv(rows)
    imported_counts, yamtrack_warnings = yamtrack.importer(csv_file, user, mode)

    warning_parts = _conversion_warnings(report)
    if yamtrack_warnings:
        warning_parts.append(yamtrack_warnings)

    return imported_counts, "\n".join(warning_parts)
