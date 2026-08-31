import logging

from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
    _serialize_media,
)
from app.models import BasicMedia, MediaTypes
from app.providers import services, tmdb


logger = logging.getLogger(__name__)


def login_not_required(view_func):
    """Mark this API view as public for login-required middleware.

    Authentication is still handled manually through the Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    auth_result = _get_authenticated_api_user(request)

    if isinstance(auth_result, JsonResponse):
        return None, auth_result

    if auth_result is None:
        return None, JsonResponse(
            {
                "detail": "Autenticação necessária.",
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


def _resolve_episode_image(episode):
    image = episode.get("image")

    if image:
        return image

    still_path = episode.get("still_path")

    if still_path:
        try:
            return tmdb.get_image_url(still_path)
        except Exception:
            return ""

    return ""


def _find_episode(metadata, episode_number):
    if not metadata or episode_number is None:
        return None

    episodes = metadata.get("episodes") or []

    for episode in episodes:
        current_number = episode.get("episode_number")

        try:
            current_number = int(current_number)
        except (TypeError, ValueError):
            continue

        if current_number == int(episode_number):
            return episode

    return None


def _serialize_episode(
    episode,
    season_number=None,
):
    if not episode:
        return None

    episode_number = episode.get("episode_number")

    title = (
        episode.get("name")
        or episode.get("title")
        or (
            f"Episódio {episode_number}"
            if episode_number is not None
            else "Episódio"
        )
    )

    synopsis = _normalize_synopsis(
        episode.get("overview")
        or episode.get("synopsis")
        or episode.get("description")
    )

    return {
        "season_number": season_number,
        "episode_number": episode_number,
        "title": title,
        "synopsis": synopsis,
        "image": _resolve_episode_image(
            episode,
        ),
        "air_date": episode.get(
            "air_date",
        ),
        "runtime": episode.get(
            "runtime",
        ),
    }


def _get_item_metadata(item):
    season_number = getattr(
        item,
        "season_number",
        None,
    )

    episode_number = getattr(
        item,
        "episode_number",
        None,
    )

    if (
        item.media_type
        == MediaTypes.EPISODE.value
        and season_number is not None
        and episode_number is not None
    ):
        season_metadata = (
            services.get_media_metadata(
                MediaTypes.SEASON.value,
                item.media_id,
                item.source,
                [season_number],
            )
        )

        episode = _find_episode(
            season_metadata,
            episode_number,
        )

        return {
            "media_id": str(
                item.media_id,
            ),
            "source": item.source,
            "media_type": (
                MediaTypes.EPISODE.value
            ),
            "title": (
                season_metadata.get(
                    "title",
                )
                or item.title
            ),
            "season_title": (
                season_metadata.get(
                    "season_title",
                    "",
                )
            ),
            "episode_title": (
                episode.get("name", "")
                if episode
                else ""
            ),
            "image": (
                _resolve_episode_image(
                    episode,
                )
                if episode
                else item.image
            ),
            "synopsis": (
                _normalize_synopsis(
                    episode.get(
                        "overview",
                    )
                )
                if episode
                else ""
            ),
            "genres": (
                season_metadata.get(
                    "genres",
                    [],
                )
            ),
            "details": {
                "season_number": (
                    season_number
                ),
                "episode_number": (
                    episode_number
                ),
                "air_date": (
                    episode.get(
                        "air_date",
                    )
                    if episode
                    else None
                ),
                "runtime": (
                    episode.get(
                        "runtime",
                    )
                    if episode
                    else None
                ),
            },
        }

    season_numbers = (
        [season_number]
        if season_number is not None
        else None
    )

    return services.get_media_metadata(
        item.media_type,
        item.media_id,
        item.source,
        season_numbers,
        episode_number,
    )


def _serialize_provider_metadata(
    metadata,
    *,
    fallback_media_id="",
    fallback_source="",
    fallback_media_type="",
    fallback_title="",
):
    metadata = metadata or {}

    title = (
        metadata.get("episode_title")
        or metadata.get("title")
        or fallback_title
    )

    return {
        "media_id": str(
            metadata.get(
                "media_id",
                fallback_media_id,
            )
        ),
        "source": (
            metadata.get("source")
            or fallback_source
        ),
        "media_type": (
            metadata.get("media_type")
            or fallback_media_type
        ),
        "title": title,
        "season_title": (
            metadata.get(
                "season_title",
                "",
            )
        ),
        "episode_title": (
            metadata.get(
                "episode_title",
                "",
            )
        ),
        "image": (
            metadata.get("image")
            or ""
        ),
        "synopsis": (
            _normalize_synopsis(
                metadata.get("synopsis")
                or metadata.get(
                    "overview"
                )
                or metadata.get(
                    "description"
                )
            )
        ),
        "genres": _api_jsonify(
            metadata.get(
                "genres",
                [],
            )
        ),
        "score": _api_jsonify(
            metadata.get("score")
        ),
        "details": _api_jsonify(
            metadata.get(
                "details",
                {},
            )
        ),
    }


def _get_next_episode_payload(
    media,
    metadata,
):
    item = media.item

    if (
        item.media_type
        != MediaTypes.SEASON.value
    ):
        return None

    season_number = getattr(
        item,
        "season_number",
        None,
    )

    next_episode_number = getattr(
        media,
        "next_episode_number",
        None,
    )

    if (
        season_number is None
        or next_episode_number is None
    ):
        return None

    episode = _find_episode(
        metadata,
        next_episode_number,
    )

    if episode is None:
        return None

    return _serialize_episode(
        episode,
        season_number=season_number,
    )


@login_not_required
@require_GET
def mobile_media_detail(
    request,
    media_type,
    instance_id,
):
    user, error_response = (
        _get_authenticated_user(
            request,
        )
    )

    if error_response:
        return error_response

    try:
        media = (
            BasicMedia.objects
            .get_media_prefetch(
                user=user,
                media_type=media_type,
                instance_id=instance_id,
            )
        )
    except ObjectDoesNotExist:
        return JsonResponse(
            {
                "detail": (
                    "Mídia não encontrada."
                ),
            },
            status=404,
        )
    except Exception as exc:
        logger.exception(
            (
                "Erro ao buscar detalhe "
                "mobile: media_type=%s "
                "instance_id=%s user=%s"
            ),
            media_type,
            instance_id,
            user.id,
        )

        return JsonResponse(
            {
                "detail": (
                    "Erro ao buscar mídia."
                ),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
            status=500,
        )

    try:
        media_payload = (
            _serialize_media(media)
        )
    except Exception as exc:
        logger.exception(
            (
                "Erro ao serializar "
                "mídia mobile: "
                "media_type=%s "
                "instance_id=%s"
            ),
            media_type,
            instance_id,
        )

        return JsonResponse(
            {
                "detail": (
                    "Erro ao preparar "
                    "os detalhes da mídia."
                ),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
            status=500,
        )

    metadata = {}

    try:
        metadata = _get_item_metadata(
            media.item,
        )
    except Exception:
        logger.exception(
            (
                "Não foi possível obter "
                "metadados do provedor "
                "para %s."
            ),
            media.item,
        )

    provider_metadata = (
        _serialize_provider_metadata(
            metadata,
            fallback_media_id=(
                media.item.media_id
            ),
            fallback_source=(
                media.item.source
            ),
            fallback_media_type=(
                media.item.media_type
            ),
            fallback_title=(
                media.item.title
            ),
        )
    )

    media_payload.update(
        {
            "synopsis": (
                provider_metadata[
                    "synopsis"
                ]
            ),
            "genres": (
                provider_metadata[
                    "genres"
                ]
            ),
            "provider_details": (
                provider_metadata[
                    "details"
                ]
            ),
            "season_title": (
                provider_metadata[
                    "season_title"
                ]
            ),
            "episode_title": (
                provider_metadata[
                    "episode_title"
                ]
            ),
            "next_episode": (
                _get_next_episode_payload(
                    media,
                    metadata,
                )
            ),
        }
    )

    return JsonResponse(
        {
            "media": _api_jsonify(
                media_payload
            ),
        },
        status=200,
    )


@login_not_required
@require_GET
def mobile_provider_detail(
    request,
    source,
    media_type,
    media_id,
):
    user, error_response = (
        _get_authenticated_user(
            request,
        )
    )

    if error_response:
        return error_response

    if media_type not in MediaTypes.values:
        return JsonResponse(
            {
                "detail": (
                    "Tipo de mídia inválido."
                ),
            },
            status=400,
        )

    try:
        metadata = (
            services.get_media_metadata(
                media_type,
                media_id,
                source,
            )
        )
    except Exception as exc:
        logger.exception(
            (
                "Erro ao buscar detalhes "
                "do provedor: source=%s "
                "media_type=%s "
                "media_id=%s user=%s"
            ),
            source,
            media_type,
            media_id,
            user.id,
        )

        return JsonResponse(
            {
                "detail": (
                    "Não foi possível "
                    "carregar os detalhes "
                    "deste conteúdo."
                ),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
            status=502,
        )

    payload = (
        _serialize_provider_metadata(
            metadata,
            fallback_media_id=media_id,
            fallback_source=source,
            fallback_media_type=(
                media_type
            ),
        )
    )

    return JsonResponse(
        {
            "media": payload,
        },
        status=200,
    )