import logging

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
    _read_json_body,
    _serialize_media,
)
from app.models import (
    BasicMedia,
    MediaTypes,
    Sources,
    Status,
)
from app.providers import (
    services,
    tmdb,
)


logger = logging.getLogger(__name__)


STANDALONE_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.BOARDGAME.value,
}


def login_not_required(view_func):
    """Mark this API view as public for login-required middleware.

    Authentication is still handled manually through the
    Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    user = _get_authenticated_api_user(
        request,
    )

    if user is None:
        return None, JsonResponse(
            {
                "detail": (
                    "Autenticação necessária."
                ),
            },
            status=401,
        )

    return user, None


def _normalize_text(value):
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


def _iso_datetime(value):
    if value is None:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _get_tracked_media(
    user,
    source,
    media_type,
    media_id,
):
    try:
        queryset = (
            BasicMedia.objects
            .filter_media_prefetch(
                user=user,
                media_id=str(
                    media_id
                ),
                media_type=media_type,
                source=source,
            )
        )

        return (
            queryset
            .order_by(
                "-created_at",
                "-pk",
            )
            .first()
        )
    except Exception:
        logger.exception(
            (
                "Não foi possível localizar "
                "mídia rastreada: "
                "user=%s source=%s "
                "media_type=%s media_id=%s"
            ),
            user.id,
            source,
            media_type,
            media_id,
        )

        return None


def _get_tmdb_backdrop(
    source,
    media_type,
    media_id,
):
    if (
        source != Sources.TMDB.value
        or media_type
        != MediaTypes.MOVIE.value
    ):
        return ""

    cache_key = (
        "mobile_work_backdrop_"
        f"{source}_"
        f"{media_type}_"
        f"{media_id}"
    )

    cached = cache.get(
        cache_key,
    )

    if cached is not None:
        return cached

    backdrop = ""

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            (
                f"{tmdb.base_url}/movie/"
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

    except Exception:
        logger.exception(
            (
                "Não foi possível obter "
                "backdrop da obra: "
                "source=%s "
                "media_type=%s "
                "media_id=%s"
            ),
            source,
            media_type,
            media_id,
        )

    cache.set(
        cache_key,
        backdrop,
        60 * 60 * 24,
    )

    return backdrop


def _get_backdrop(
    metadata,
    source,
    media_type,
    media_id,
):
    metadata_backdrop = (
        metadata.get(
            "backdrop"
        )
        or metadata.get(
            "backdrop_url"
        )
        or metadata.get(
            "banner"
        )
        or ""
    )

    if metadata_backdrop:
        return metadata_backdrop

    return _get_tmdb_backdrop(
        source,
        media_type,
        media_id,
    )


def _serialize_tracking(
    tracked_media,
):
    if tracked_media is None:
        return None

    try:
        payload = (
            _serialize_media(
                tracked_media
            )
        )
    except Exception:
        logger.exception(
            (
                "Falha ao serializar "
                "tracking da obra %s."
            ),
            tracked_media,
        )

        payload = {
            "id": tracked_media.pk,
            "status": getattr(
                tracked_media,
                "status",
                None,
            ),
            "score": getattr(
                tracked_media,
                "score",
                None,
            ),
            "progress": getattr(
                tracked_media,
                "progress",
                None,
            ),
            "start_date": (
                _iso_datetime(
                    getattr(
                        tracked_media,
                        "start_date",
                        None,
                    )
                )
            ),
            "end_date": (
                _iso_datetime(
                    getattr(
                        tracked_media,
                        "end_date",
                        None,
                    )
                )
            ),
        }

    return _api_jsonify(
        payload
    )


def _build_work_payload(
    metadata,
    tracked_media,
    *,
    source,
    media_type,
    media_id,
):
    metadata = metadata or {}

    tracking = (
        _serialize_tracking(
            tracked_media
        )
    )

    status = (
        getattr(
            tracked_media,
            "status",
            None,
        )
        if tracked_media
        is not None
        else None
    )

    watched = (
        media_type
        == MediaTypes.MOVIE.value
        and status
        == Status.COMPLETED.value
    )

    return {
        "media_id": str(
            metadata.get(
                "media_id",
                media_id,
            )
        ),
        "source": (
            metadata.get(
                "source"
            )
            or source
        ),
        "media_type": (
            metadata.get(
                "media_type"
            )
            or media_type
        ),
        "title": (
            metadata.get(
                "title"
            )
            or ""
        ),
        "image": (
            metadata.get(
                "image"
            )
            or ""
        ),
        "backdrop": (
            _get_backdrop(
                metadata,
                source,
                media_type,
                media_id,
            )
        ),
        "synopsis": (
            _normalize_text(
                metadata.get(
                    "synopsis"
                )
                or metadata.get(
                    "overview"
                )
                or metadata.get(
                    "description"
                )
            )
        ),
        "genres": (
            _api_jsonify(
                metadata.get(
                    "genres",
                    [],
                )
            )
        ),
        "score": (
            _api_jsonify(
                metadata.get(
                    "score"
                )
            )
        ),
        "score_count": (
            _api_jsonify(
                metadata.get(
                    "score_count"
                )
            )
        ),
        "details": (
            _api_jsonify(
                metadata.get(
                    "details",
                    {},
                )
            )
        ),
        "cast": (
            _api_jsonify(
                metadata.get(
                    "cast",
                    [],
                )
            )
        ),
        "total_cast_count": (
            _api_jsonify(
                metadata.get(
                    "total_cast_count"
                )
            )
        ),
        "related": (
            _api_jsonify(
                metadata.get(
                    "related",
                    {},
                )
            )
        ),
        "external_links": (
            _api_jsonify(
                metadata.get(
                    "external_links",
                    {},
                )
            )
        ),
        "providers": (
            _api_jsonify(
                metadata.get(
                    "providers",
                    {},
                )
            )
        ),
        "source_url": (
            metadata.get(
                "source_url"
            )
            or ""
        ),
        "is_tracked": (
            tracked_media
            is not None
        ),
        "tracking": tracking,
        "watched": watched,
        "watched_at": (
            _iso_datetime(
                getattr(
                    tracked_media,
                    "end_date",
                    None,
                )
            )
            if watched
            and tracked_media
            is not None
            else None
        ),
        "can_toggle_watched": (
            media_type
            == MediaTypes.MOVIE.value
            and tracked_media
            is not None
        ),
    }


def _update_movie_watched(
    tracked_movie,
    watched,
):
    now = (
        timezone.now()
        .replace(
            second=0,
            microsecond=0,
        )
    )

    if watched:
        tracked_movie.status = (
            Status.COMPLETED.value
        )

        tracked_movie.progress = 1

        if (
            tracked_movie.start_date
            is None
        ):
            tracked_movie.start_date = (
                now
            )

        tracked_movie.end_date = (
            now
        )

    else:
        tracked_movie.status = (
            Status.PLANNING.value
        )

        tracked_movie.progress = 0
        tracked_movie.start_date = None
        tracked_movie.end_date = None

    tracked_movie.save()


@login_not_required
@csrf_exempt
@require_http_methods(
    ["GET", "POST"]
)
def mobile_work_detail(
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

    if (
        media_type
        not in STANDALONE_MEDIA_TYPES
    ):
        return JsonResponse(
            {
                "detail": (
                    "Este tipo de mídia "
                    "ainda não utiliza "
                    "a tela de obra."
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
                "Erro ao buscar obra: "
                "source=%s "
                "media_type=%s "
                "media_id=%s "
                "user=%s"
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

    tracked_media = (
        _get_tracked_media(
            user,
            source,
            media_type,
            media_id,
        )
    )

    if request.method == "POST":
        if (
            media_type
            != MediaTypes.MOVIE.value
        ):
            return JsonResponse(
                {
                    "detail": (
                        "A alteração de "
                        "status ainda não "
                        "está disponível "
                        "para este tipo "
                        "de mídia."
                    ),
                },
                status=400,
            )

        if tracked_media is None:
            return JsonResponse(
                {
                    "detail": (
                        "Adicione este "
                        "filme à sua "
                        "biblioteca antes "
                        "de registrar "
                        "como assistido."
                    ),
                },
                status=409,
            )

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
            _update_movie_watched(
                tracked_media,
                watched,
            )
        except Exception as exc:
            logger.exception(
                (
                    "Erro ao atualizar "
                    "filme: "
                    "media_id=%s "
                    "user=%s"
                ),
                media_id,
                user.id,
            )

            return JsonResponse(
                {
                    "detail": (
                        "Não foi possível "
                        "atualizar este "
                        "filme."
                    ),
                    "error_type": (
                        exc.__class__.__name__
                    ),
                },
                status=500,
            )

        tracked_media = (
            _get_tracked_media(
                user,
                source,
                media_type,
                media_id,
            )
        )

        payload = (
            _build_work_payload(
                metadata,
                tracked_media,
                source=source,
                media_type=(
                    media_type
                ),
                media_id=media_id,
            )
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Filme marcado "
                    "como assistido."
                    if watched
                    else (
                        "Filme marcado "
                        "como não "
                        "assistido."
                    )
                ),
                "media": (
                    _api_jsonify(
                        payload
                    )
                ),
            },
            status=200,
        )

    payload = (
        _build_work_payload(
            metadata,
            tracked_media,
            source=source,
            media_type=media_type,
            media_id=media_id,
        )
    )

    return JsonResponse(
        {
            "media": (
                _api_jsonify(
                    payload
                )
            ),
        },
        status=200,
    )