import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from api.mobile_series import (
    _episode_image,
    _get_backdrop,
    _get_tracked_tv,
    _normalize_synopsis,
)
from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
    _read_json_body,
)
from app.models import (
    Episode,
    Item,
    MediaTypes,
    Season,
    Status,
)
from app.providers import services


logger = logging.getLogger(__name__)


def login_not_required(view_func):
    """Mark this API view as public for login-required middleware.

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


def _get_provider_data(
    source,
    media_id,
    season_number,
):
    tv_metadata = (
        services.get_media_metadata(
            MediaTypes.TV.value,
            media_id,
            source,
        )
    )

    season_metadata = (
        services.get_media_metadata(
            MediaTypes.SEASON.value,
            media_id,
            source,
            [season_number],
        )
    )

    return (
        tv_metadata,
        season_metadata,
    )


def _find_provider_episode(
    season_metadata,
    episode_number,
):
    episodes = (
        season_metadata.get(
            "episodes",
            [],
        )
        or []
    )

    for episode in episodes:
        current_number = (
            _safe_int(
                episode.get(
                    "episode_number"
                )
            )
        )

        if (
            current_number
            == episode_number
        ):
            return episode

    return None


def _find_episode_index(
    episodes,
    episode_number,
):
    for index, episode in enumerate(
        episodes
    ):
        current_number = (
            _safe_int(
                episode.get(
                    "episode_number"
                )
            )
        )

        if (
            current_number
            == episode_number
        ):
            return index

    return None


def _find_tracked_season(
    tracked_tv,
    season_number,
):
    if tracked_tv is None:
        return None

    return (
        Season.objects
        .filter(
            related_tv=tracked_tv,
            item__season_number=(
                season_number
            ),
        )
        .select_related(
            "item",
            "related_tv",
        )
        .first()
    )


def _find_tracked_episode(
    tracked_season,
    episode_number,
):
    if tracked_season is None:
        return None

    return (
        Episode.objects
        .filter(
            related_season=(
                tracked_season
            ),
            item__episode_number=(
                episode_number
            ),
        )
        .select_related(
            "item",
            "related_season",
        )
        .order_by(
            "-end_date",
            "-created_at",
            "-pk",
        )
        .first()
    )


def _serialize_navigation_episode(
    episode,
):
    if episode is None:
        return None

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

    return {
        "episode_number": (
            episode_number
        ),
        "title": title,
    }


def _episode_navigation(
    season_metadata,
    episode_number,
):
    episodes = (
        season_metadata.get(
            "episodes",
            [],
        )
        or []
    )

    current_index = (
        _find_episode_index(
            episodes,
            episode_number,
        )
    )

    if current_index is None:
        return None, None

    previous_episode = None
    next_episode = None

    if current_index > 0:
        previous_episode = (
            episodes[
                current_index - 1
            ]
        )

    if (
        current_index
        < len(episodes) - 1
    ):
        next_episode = (
            episodes[
                current_index + 1
            ]
        )

    return (
        _serialize_navigation_episode(
            previous_episode,
        ),
        _serialize_navigation_episode(
            next_episode,
        ),
    )


def _episode_title(
    episode,
    episode_number,
):
    return (
        episode.get("title")
        or episode.get("name")
        or (
            f"Episódio "
            f"{episode_number}"
        )
    )


def _build_episode_payload(
    user,
    source,
    media_id,
    season_number,
    episode_number,
):
    (
        tv_metadata,
        season_metadata,
    ) = _get_provider_data(
        source,
        media_id,
        season_number,
    )

    provider_episode = (
        _find_provider_episode(
            season_metadata,
            episode_number,
        )
    )

    if provider_episode is None:
        return None

    tracked_tv = (
        _get_tracked_tv(
            user,
            source,
            media_id,
        )
    )

    tracked_season = (
        _find_tracked_season(
            tracked_tv,
            season_number,
        )
    )

    tracked_episode = (
        _find_tracked_episode(
            tracked_season,
            episode_number,
        )
    )

    (
        previous_episode,
        next_episode,
    ) = _episode_navigation(
        season_metadata,
        episode_number,
    )

    series_title = (
        tv_metadata.get(
            "title"
        )
        or season_metadata.get(
            "title"
        )
        or ""
    )

    series_image = (
        tv_metadata.get(
            "image"
        )
        or ""
    )

    season_title = (
        season_metadata.get(
            "season_title"
        )
        or (
            "Especiais"
            if season_number == 0
            else (
                f"Temporada "
                f"{season_number}"
            )
        )
    )

    synopsis = (
        _normalize_synopsis(
            provider_episode.get(
                "overview"
            )
            or provider_episode.get(
                "synopsis"
            )
            or provider_episode.get(
                "description"
            )
        )
    )

    payload = {
        "series": {
            "id": (
                tracked_tv.pk
                if tracked_tv
                is not None
                else None
            ),
            "media_id": str(
                media_id
            ),
            "source": source,
            "title": (
                series_title
            ),
            "image": (
                series_image
            ),
            "backdrop": (
                _get_backdrop(
                    source,
                    media_id,
                )
            ),
            "is_tracked": (
                tracked_tv
                is not None
            ),
        },
        "season": {
            "season_number": (
                season_number
            ),
            "title": (
                season_title
            ),
            "instance_id": (
                tracked_season.pk
                if tracked_season
                is not None
                else None
            ),
            "is_tracked": (
                tracked_season
                is not None
            ),
        },
        "episode": {
            "episode_number": (
                episode_number
            ),
            "title": (
                _episode_title(
                    provider_episode,
                    episode_number,
                )
            ),
            "synopsis": synopsis,
            "image": (
                _episode_image(
                    provider_episode
                )
            ),
            "air_date": (
                provider_episode.get(
                    "air_date"
                )
            ),
            "runtime": (
                provider_episode.get(
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
        },
        "previous_episode": (
            previous_episode
        ),
        "next_episode": (
            next_episode
        ),
        "can_toggle_watched": (
            tracked_tv
            is not None
        ),
    }

    return payload


def _ensure_tracked_season(
    tracked_tv,
    source,
    media_id,
    season_number,
    tv_metadata,
    season_metadata,
):
    tracked_season = (
        _find_tracked_season(
            tracked_tv,
            season_number,
        )
    )

    if tracked_season is not None:
        return tracked_season

    series_title = (
        tv_metadata.get(
            "title"
        )
        or season_metadata.get(
            "title"
        )
        or ""
    )

    season_image = (
        season_metadata.get(
            "image"
        )
        or tv_metadata.get(
            "image"
        )
        or ""
    )

    season_item, _ = (
        Item.objects.get_or_create(
            media_id=str(
                media_id
            ),
            source=source,
            media_type=(
                MediaTypes.SEASON.value
            ),
            season_number=(
                season_number
            ),
            episode_number=None,
            defaults={
                "title": (
                    series_title
                ),
                "image": (
                    season_image
                ),
            },
        )
    )

    tracked_season = Season(
        item=season_item,
        user=tracked_tv.user,
        related_tv=tracked_tv,
        status=(
            Status.IN_PROGRESS.value
        ),
        score=None,
        notes="",
    )

    tracked_season.save()

    return tracked_season


def _set_episode_watched(
    user,
    source,
    media_id,
    season_number,
    episode_number,
    watched,
):
    (
        tv_metadata,
        season_metadata,
    ) = _get_provider_data(
        source,
        media_id,
        season_number,
    )

    provider_episode = (
        _find_provider_episode(
            season_metadata,
            episode_number,
        )
    )

    if provider_episode is None:
        return (
            None,
            JsonResponse(
                {
                    "detail": (
                        "Episódio não "
                        "encontrado."
                    ),
                },
                status=404,
            ),
        )

    tracked_tv = (
        _get_tracked_tv(
            user,
            source,
            media_id,
        )
    )

    if tracked_tv is None:
        return (
            None,
            JsonResponse(
                {
                    "detail": (
                        "Adicione esta "
                        "série à sua "
                        "biblioteca antes "
                        "de registrar "
                        "episódios "
                        "assistidos."
                    ),
                },
                status=409,
            ),
        )

    tracked_season = (
        _find_tracked_season(
            tracked_tv,
            season_number,
        )
    )

    if watched:
        tracked_season = (
            _ensure_tracked_season(
                tracked_tv,
                source,
                media_id,
                season_number,
                tv_metadata,
                season_metadata,
            )
        )

        tracked_episode = (
            _find_tracked_episode(
                tracked_season,
                episode_number,
            )
        )

        if tracked_episode is None:
            now = (
                timezone.now()
                .replace(
                    second=0,
                    microsecond=0,
                )
            )

            tracked_season.watch(
                episode_number,
                now,
            )

    else:
        if tracked_season is not None:
            tracked_episode = (
                _find_tracked_episode(
                    tracked_season,
                    episode_number,
                )
            )

            if tracked_episode is not None:
                tracked_season.unwatch(
                    episode_number,
                )

    payload = (
        _build_episode_payload(
            user,
            source,
            media_id,
            season_number,
            episode_number,
        )
    )

    return payload, None


@login_not_required
@csrf_exempt
@require_http_methods(
    ["GET", "POST"]
)
def mobile_episode_detail(
    request,
    source,
    media_id,
    season_number,
    episode_number,
):
    user, error_response = (
        _get_authenticated_user(
            request,
        )
    )

    if error_response:
        return error_response

    if (
        season_number < 0
        or episode_number <= 0
    ):
        return JsonResponse(
            {
                "detail": (
                    "Temporada ou "
                    "episódio inválido."
                ),
            },
            status=400,
        )

    if request.method == "POST":
        data = _read_json_body(
            request
        )

        if data is None:
            return JsonResponse(
                {
                    "detail": (
                        "Envie um JSON "
                        "válido."
                    ),
                },
                status=400,
            )

        watched = data.get(
            "watched"
        )

        if not isinstance(
            watched,
            bool,
        ):
            return JsonResponse(
                {
                    "detail": (
                        "watched deve ser "
                        "true ou false."
                    ),
                },
                status=400,
            )

        try:
            (
                payload,
                action_error,
            ) = _set_episode_watched(
                user,
                source,
                media_id,
                season_number,
                episode_number,
                watched,
            )
        except Exception as exc:
            logger.exception(
                (
                    "Erro ao atualizar "
                    "episódio mobile: "
                    "source=%s "
                    "media_id=%s "
                    "season=%s "
                    "episode=%s "
                    "user=%s"
                ),
                source,
                media_id,
                season_number,
                episode_number,
                user.id,
            )

            return JsonResponse(
                {
                    "detail": (
                        "Não foi possível "
                        "atualizar este "
                        "episódio."
                    ),
                    "error_type": (
                        exc.__class__.__name__
                    ),
                },
                status=500,
            )

        if action_error:
            return action_error

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Episódio marcado "
                    "como assistido."
                    if watched
                    else (
                        "Episódio marcado "
                        "como não "
                        "assistido."
                    )
                ),
                **_api_jsonify(
                    payload
                ),
            },
            status=200,
        )

    try:
        payload = (
            _build_episode_payload(
                user,
                source,
                media_id,
                season_number,
                episode_number,
            )
        )
    except Exception as exc:
        logger.exception(
            (
                "Erro ao carregar "
                "episódio mobile: "
                "source=%s "
                "media_id=%s "
                "season=%s "
                "episode=%s "
                "user=%s"
            ),
            source,
            media_id,
            season_number,
            episode_number,
            user.id,
        )

        return JsonResponse(
            {
                "detail": (
                    "Não foi possível "
                    "carregar este "
                    "episódio."
                ),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
            status=502,
        )

    if payload is None:
        return JsonResponse(
            {
                "detail": (
                    "Episódio não "
                    "encontrado."
                ),
            },
            status=404,
        )

    return JsonResponse(
        _api_jsonify(
            payload
        ),
        status=200,
    )