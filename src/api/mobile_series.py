import logging

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
)
from app.models import (
    MediaTypes,
    Sources,
    TV,
)
from app.providers import (
    services,
    tmdb,
)


logger = logging.getLogger(__name__)


def login_not_required(view_func):
    """Mark the API view as public for login-required middleware.

    Authentication is still handled manually through the
    Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    auth_result = (
        _get_authenticated_api_user(
            request,
        )
    )

    if isinstance(
        auth_result,
        JsonResponse,
    ):
        return None, auth_result

    if auth_result is None:
        return None, JsonResponse(
            {
                "detail": (
                    "Autenticação necessária."
                ),
            },
            status=401,
        )

    return auth_result, None


def _normalize_synopsis(value):
    if value is None:
        return ""

    text = str(value).strip()

    if text in {
        "",
        "No synopsis available.",
        "No description available.",
    }:
        return ""

    return text


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_datetime(value):
    if value is None:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _get_backdrop(
    source,
    media_id,
):
    """
    Return a wide backdrop for the mobile hero.

    TMDB's normal Yamtrack metadata currently stores the poster,
    so the mobile series screen requests and caches the backdrop
    separately.
    """
    if source != Sources.TMDB.value:
        return ""

    cache_key = (
        "mobile_series_backdrop_"
        f"{source}_{media_id}"
    )

    cached = cache.get(
        cache_key,
    )

    if cached is not None:
        return cached

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            (
                f"{tmdb.base_url}/tv/"
                f"{media_id}"
            ),
            params={
                **tmdb.base_params,
            },
        )

        backdrop_path = (
            response.get(
                "backdrop_path"
            )
        )

        if backdrop_path:
            backdrop = (
                "https://image.tmdb.org/"
                "t/p/w1280"
                f"{backdrop_path}"
            )
        else:
            backdrop = ""

    except Exception:
        logger.exception(
            (
                "Não foi possível obter "
                "backdrop da série "
                "source=%s media_id=%s"
            ),
            source,
            media_id,
        )

        backdrop = ""

    cache.set(
        cache_key,
        backdrop,
        60 * 60 * 24,
    )

    return backdrop


def _get_tracked_tv(
    user,
    source,
    media_id,
):
    return (
        TV.objects
        .filter(
            user=user,
            item__source=source,
            item__media_id=str(
                media_id,
            ),
        )
        .select_related(
            "item",
        )
        .prefetch_related(
            "seasons__item",
            "seasons__episodes__item",
        )
        .order_by(
            "-created_at",
            "-pk",
        )
        .first()
    )


def _get_provider_seasons(
    tv_metadata,
):
    related = (
        tv_metadata.get(
            "related",
            {},
        )
        or {}
    )

    seasons = (
        related.get(
            "seasons",
            [],
        )
        or []
    )

    return seasons


def _collect_season_numbers(
    tv_metadata,
    tracked_tv,
):
    numbers = set()

    for season in (
        _get_provider_seasons(
            tv_metadata
        )
    ):
        number = _safe_int(
            season.get(
                "season_number"
            )
        )

        if number is not None:
            numbers.add(number)

    if tracked_tv is not None:
        for season in (
            tracked_tv.seasons.all()
        ):
            number = (
                season.item.season_number
            )

            if number is not None:
                numbers.add(
                    int(number)
                )

    return sorted(
        numbers,
        key=lambda value: (
            value == 0,
            value,
        ),
    )


def _tracked_seasons_map(
    tracked_tv,
):
    if tracked_tv is None:
        return {}

    result = {}

    for season in (
        tracked_tv.seasons.all()
    ):
        number = (
            season.item.season_number
        )

        if number is None:
            continue

        result[int(number)] = season

    return result


def _tracked_episodes_map(
    tracked_season,
):
    if tracked_season is None:
        return {}

    result = {}

    for episode in (
        tracked_season.episodes.all()
    ):
        number = (
            episode.item.episode_number
        )

        if number is None:
            continue

        result[int(number)] = episode

    return result


def _find_provider_season_summary(
    tv_metadata,
    season_number,
):
    for season in (
        _get_provider_seasons(
            tv_metadata
        )
    ):
        current_number = (
            _safe_int(
                season.get(
                    "season_number"
                )
            )
        )

        if (
            current_number
            == season_number
        ):
            return season

    return {}


def _episode_image(episode):
    image = (
        episode.get("image")
        or ""
    )

    if image:
        return image

    still_path = (
        episode.get(
            "still_path"
        )
    )

    if still_path:
        try:
            return (
                tmdb.get_image_url(
                    still_path
                )
            )
        except Exception:
            return ""

    return ""


def _serialize_episode(
    episode,
    tracked_episode,
):
    episode_number = (
        _safe_int(
            episode.get(
                "episode_number"
            )
        )
    )

    title = (
        episode.get("title")
        or episode.get("name")
        or (
            f"Episódio "
            f"{episode_number}"
            if episode_number
            is not None
            else "Episódio"
        )
    )

    synopsis = (
        _normalize_synopsis(
            episode.get(
                "overview"
            )
            or episode.get(
                "synopsis"
            )
            or episode.get(
                "description"
            )
        )
    )

    return {
        "episode_number": (
            episode_number
        ),
        "title": title,
        "synopsis": synopsis,
        "image": _episode_image(
            episode
        ),
        "air_date": (
            episode.get(
                "air_date"
            )
        ),
        "runtime": (
            episode.get(
                "runtime"
            )
        ),
        "watched": (
            tracked_episode
            is not None
        ),
        "watched_at": (
            _iso_datetime(
                tracked_episode.end_date
            )
            if tracked_episode
            is not None
            else None
        ),
        "instance_id": (
            tracked_episode.pk
            if tracked_episode
            is not None
            else None
        ),
    }


def _serialize_season(
    season_number,
    season_metadata,
    season_summary,
    tracked_season,
):
    tracked_episodes = (
        _tracked_episodes_map(
            tracked_season
        )
    )

    provider_episodes = (
        season_metadata.get(
            "episodes",
            [],
        )
        or []
    )

    episodes = []

    for episode in provider_episodes:
        episode_number = (
            _safe_int(
                episode.get(
                    "episode_number"
                )
            )
        )

        tracked_episode = (
            tracked_episodes.get(
                episode_number
            )
            if episode_number
            is not None
            else None
        )

        episodes.append(
            _serialize_episode(
                episode,
                tracked_episode,
            )
        )

    provider_total = (
        _safe_int(
            season_metadata.get(
                "max_progress"
            )
        )
    )

    if provider_total is None:
        provider_total = (
            _safe_int(
                season_summary.get(
                    "max_progress"
                )
            )
        )

    if provider_total is None:
        provider_total = len(
            provider_episodes
        )

    watched_count = len(
        tracked_episodes
    )

    progress_percent = 0

    if provider_total > 0:
        progress_percent = round(
            min(
                (
                    watched_count
                    / provider_total
                )
                * 100,
                100,
            ),
            1,
        )

    if season_number == 0:
        default_title = (
            "Especiais"
        )
    else:
        default_title = (
            f"Temporada "
            f"{season_number}"
        )

    season_title = (
        season_metadata.get(
            "season_title"
        )
        or season_summary.get(
            "season_title"
        )
        or default_title
    )

    image = (
        season_metadata.get(
            "image"
        )
        or season_summary.get(
            "image"
        )
        or ""
    )

    synopsis = (
        _normalize_synopsis(
            season_metadata.get(
                "synopsis"
            )
            or season_summary.get(
                "synopsis"
            )
        )
    )

    return {
        "season_number": (
            season_number
        ),
        "title": season_title,
        "image": image,
        "synopsis": synopsis,
        "episode_count": (
            provider_total
        ),
        "watched_count": (
            watched_count
        ),
        "progress_percent": (
            progress_percent
        ),
        "is_tracked": (
            tracked_season
            is not None
        ),
        "instance_id": (
            tracked_season.pk
            if tracked_season
            is not None
            else None
        ),
        "status": (
            tracked_season.status
            if tracked_season
            is not None
            else None
        ),
        "episodes": episodes,
    }


def _build_seasons(
    tv_metadata,
    metadata_with_seasons,
    season_numbers,
    tracked_tv,
):
    tracked_seasons = (
        _tracked_seasons_map(
            tracked_tv
        )
    )

    seasons = []

    for season_number in (
        season_numbers
    ):
        season_key = (
            f"season/"
            f"{season_number}"
        )

        season_metadata = (
            metadata_with_seasons.get(
                season_key,
                {},
            )
            or {}
        )

        season_summary = (
            _find_provider_season_summary(
                tv_metadata,
                season_number,
            )
        )

        tracked_season = (
            tracked_seasons.get(
                season_number
            )
        )

        seasons.append(
            _serialize_season(
                season_number,
                season_metadata,
                season_summary,
                tracked_season,
            )
        )

    return seasons


def _regular_episode_total(
    seasons,
):
    return sum(
        season.get(
            "episode_count",
            0,
        )
        for season in seasons
        if season.get(
            "season_number"
        )
        != 0
    )


def _regular_watched_total(
    seasons,
):
    return sum(
        season.get(
            "watched_count",
            0,
        )
        for season in seasons
        if season.get(
            "season_number"
        )
        != 0
    )


@login_not_required
@require_GET
def mobile_series_detail(
    request,
    source,
    media_id,
):
    user, error_response = (
        _get_authenticated_user(
            request
        )
    )

    if error_response:
        return error_response

    try:
        tv_metadata = (
            services.get_media_metadata(
                MediaTypes.TV.value,
                media_id,
                source,
            )
        )
    except Exception as exc:
        logger.exception(
            (
                "Erro ao buscar série "
                "para mobile: "
                "source=%s "
                "media_id=%s "
                "user=%s"
            ),
            source,
            media_id,
            user.id,
        )

        return JsonResponse(
            {
                "detail": (
                    "Não foi possível "
                    "carregar esta série."
                ),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
            status=502,
        )

    tracked_tv = (
        _get_tracked_tv(
            user,
            source,
            media_id,
        )
    )

    season_numbers = (
        _collect_season_numbers(
            tv_metadata,
            tracked_tv,
        )
    )

    metadata_with_seasons = (
        tv_metadata
    )

    if season_numbers:
        try:
            metadata_with_seasons = (
                services.get_media_metadata(
                    "tv_with_seasons",
                    media_id,
                    source,
                    season_numbers,
                )
            )
        except Exception as exc:
            logger.exception(
                (
                    "Erro ao carregar "
                    "temporadas da série "
                    "source=%s "
                    "media_id=%s "
                    "user=%s"
                ),
                source,
                media_id,
                user.id,
            )

            return JsonResponse(
                {
                    "detail": (
                        "A série foi encontrada, "
                        "mas não foi possível "
                        "carregar suas temporadas."
                    ),
                    "error_type": (
                        exc.__class__.__name__
                    ),
                },
                status=502,
            )

    seasons = _build_seasons(
        tv_metadata,
        metadata_with_seasons,
        season_numbers,
        tracked_tv,
    )

    total_episodes = (
        _regular_episode_total(
            seasons
        )
    )

    watched_episodes = (
        _regular_watched_total(
            seasons
        )
    )

    progress_percent = 0

    if total_episodes > 0:
        progress_percent = round(
            min(
                (
                    watched_episodes
                    / total_episodes
                )
                * 100,
                100,
            ),
            1,
        )

    details = (
        tv_metadata.get(
            "details",
            {},
        )
        or {}
    )

    payload = {
        "id": (
            tracked_tv.pk
            if tracked_tv
            is not None
            else None
        ),
        "media_id": str(
            tv_metadata.get(
                "media_id",
                media_id,
            )
        ),
        "source": (
            tv_metadata.get(
                "source"
            )
            or source
        ),
        "media_type": (
            MediaTypes.TV.value
        ),
        "title": (
            tv_metadata.get(
                "title"
            )
            or ""
        ),
        "image": (
            tv_metadata.get(
                "image"
            )
            or ""
        ),
        "backdrop": (
            _get_backdrop(
                source,
                media_id,
            )
        ),
        "synopsis": (
            _normalize_synopsis(
                tv_metadata.get(
                    "synopsis"
                )
            )
        ),
        "genres": _api_jsonify(
            tv_metadata.get(
                "genres",
                [],
            )
        ),
        "score": _api_jsonify(
            tv_metadata.get(
                "score"
            )
        ),
        "details": (
            _api_jsonify(
                details
            )
        ),
        "is_tracked": (
            tracked_tv
            is not None
        ),
        "status": (
            tracked_tv.status
            if tracked_tv
            is not None
            else None
        ),
        "season_count": len(
            [
                season
                for season in seasons
                if season[
                    "season_number"
                ]
                != 0
            ]
        ),
        "episode_count": (
            total_episodes
        ),
        "watched_episodes": (
            watched_episodes
        ),
        "progress_percent": (
            progress_percent
        ),
        "seasons": seasons,
    }

    return JsonResponse(
        {
            "series": _api_jsonify(
                payload
            ),
        },
        status=200,
    )