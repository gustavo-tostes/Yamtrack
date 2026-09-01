from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from api.views import (
    _get_authenticated_api_user,
    _read_json_body,
    _serialize_media,
)
from app.models import (
    BasicMedia,
    MediaTypes,
    Status,
)


def login_not_required(view_func):
    view_func.login_required = False
    return view_func


def _error(message, status=400):
    return JsonResponse(
        {
            "detail": message,
        },
        status=status,
    )


def _update_movie_progress(
    media,
    operation,
):
    now = (
        timezone.now()
        .replace(
            second=0,
            microsecond=0,
        )
    )

    if operation == "increase":
        media.status = (
            Status.COMPLETED.value
        )

        if hasattr(
            media,
            "progress",
        ):
            media.progress = 1

        if (
            hasattr(
                media,
                "start_date",
            )
            and media.start_date
            is None
        ):
            media.start_date = now

        if hasattr(
            media,
            "end_date",
        ):
            media.end_date = now

    else:
        media.status = (
            Status.PLANNING.value
        )

        if hasattr(
            media,
            "progress",
        ):
            media.progress = 0

        if hasattr(
            media,
            "start_date",
        ):
            media.start_date = None

        if hasattr(
            media,
            "end_date",
        ):
            media.end_date = None

    media.save()


@login_not_required
@csrf_exempt
@require_POST
def mobile_media_progress(
    request,
    media_type,
    instance_id,
):
    user = (
        _get_authenticated_api_user(
            request,
        )
    )

    if user is None:
        return _error(
            "Autenticação necessária.",
            status=401,
        )

    if (
        media_type
        not in MediaTypes.values
    ):
        return _error(
            "Tipo de mídia inválido.",
            status=400,
        )

    data = _read_json_body(
        request,
    )

    if data is None:
        return _error(
            "Envie um JSON válido.",
            status=400,
        )

    operation = str(
        data.get("operation")
        or "increase"
    ).strip()

    if operation not in {
        "increase",
        "decrease",
    }:
        return _error(
            "Operação inválida.",
            status=400,
        )

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
        return _error(
            "Mídia não encontrada.",
            status=404,
        )

    try:
        if (
            media_type
            == MediaTypes.MOVIE.value
        ):
            _update_movie_progress(
                media,
                operation,
            )
        elif (
            operation
            == "increase"
        ):
            media.increase_progress()
        else:
            media.decrease_progress()

    except Exception:
        return _error(
            (
                "Não foi possível "
                "atualizar o progresso "
                "desta mídia."
            ),
            status=400,
        )

    try:
        updated_media = (
            BasicMedia.objects
            .get_media_prefetch(
                user=user,
                media_type=media_type,
                instance_id=instance_id,
            )
        )
    except ObjectDoesNotExist:
        updated_media = media

    return JsonResponse(
        {
            "media": (
                _serialize_media(
                    updated_media
                )
            ),
        },
        status=200,
    )