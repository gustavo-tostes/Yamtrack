import logging

from django.apps import apps
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
    _read_json_body,
)
from app.models import MediaTypes, Status


logger = logging.getLogger(__name__)

SUPPORTED_MEDIA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.BOARDGAME.value,
}

STATUS_LABELS = {
    Status.COMPLETED.value: "Concluído",
    Status.IN_PROGRESS.value: "Em andamento",
    Status.PLANNING.value: "Planejado",
    Status.PAUSED.value: "Pausado",
    Status.DROPPED.value: "Abandonado",
}

BOOK_STATUS_LABELS = {
    Status.COMPLETED.value: "Concluído",
    Status.IN_PROGRESS.value: "Lendo",
    Status.PLANNING.value: "Quero ler",
    Status.PAUSED.value: "Pausado",
    Status.DROPPED.value: "Abandonado",
}

MOVIE_STATUS_LABELS = {
    Status.COMPLETED.value: "Assistido",
    Status.IN_PROGRESS.value: "Em andamento",
    Status.PLANNING.value: "Planejado",
    Status.PAUSED.value: "Pausado",
    Status.DROPPED.value: "Abandonado",
}


def login_not_required(view_func):
    """Mark the API view as public for login-required middleware.

    Authentication is still handled manually through the Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    user = _get_authenticated_api_user(request)

    if user is None:
        return None, JsonResponse(
            {"detail": "Autenticação necessária."},
            status=401,
        )

    return user, None


def _status_label(media_type, status):
    if media_type == MediaTypes.BOOK.value:
        return BOOK_STATUS_LABELS.get(status, status)

    if media_type == MediaTypes.MOVIE.value:
        return MOVIE_STATUS_LABELS.get(status, status)

    return STATUS_LABELS.get(status, status)


def _get_owned_media(user, media_type, instance_id):
    if media_type not in SUPPORTED_MEDIA_TYPES:
        return None

    try:
        model = apps.get_model(
            app_label="app",
            model_name=media_type,
        )
    except LookupError:
        return None

    if model is None:
        return None

    try:
        return (
            model.objects.select_related("item")
            .filter(
                pk=instance_id,
                user=user,
            )
            .first()
        )
    except Exception:
        logger.exception(
            "Falha ao localizar mídia do usuário: user=%s media_type=%s instance_id=%s",
            user.id,
            media_type,
            instance_id,
        )
        return None


def _serialize_tracking(media, media_type):
    progress = getattr(media, "progress", 0)
    formatted_progress = getattr(media, "formatted_progress", None)

    if callable(formatted_progress):
        formatted_progress = formatted_progress()

    payload = {
        "id": media.pk,
        "media_type": media_type,
        "media_id": media.item.media_id,
        "source": media.item.source,
        "title": media.item.title,
        "image": media.item.image,
        "status": media.status,
        "status_label": _status_label(media_type, media.status),
        "progress": progress,
        "formatted_progress": formatted_progress,
        "score": getattr(media, "formatted_score", getattr(media, "score", None)),
        "notes": getattr(media, "notes", ""),
        "start_date": getattr(media, "start_date", None),
        "end_date": getattr(media, "end_date", None),
    }

    return _api_jsonify(payload)


@login_not_required
@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def mobile_tracking_detail(request, media_type, instance_id):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    if media_type not in SUPPORTED_MEDIA_TYPES:
        return JsonResponse(
            {"detail": "Este tipo de mídia ainda não pode ser gerenciado pelo aplicativo."},
            status=400,
        )

    media = _get_owned_media(
        user,
        media_type,
        instance_id,
    )

    if media is None:
        return JsonResponse(
            {"detail": "Conteúdo não encontrado na sua biblioteca."},
            status=404,
        )

    if request.method == "DELETE":
        title = media.item.title

        try:
            media.delete()
        except Exception as exc:
            logger.exception(
                "Erro ao excluir mídia: user=%s media_type=%s instance_id=%s",
                user.id,
                media_type,
                instance_id,
            )
            return JsonResponse(
                {
                    "detail": "Não foi possível remover este conteúdo da sua biblioteca.",
                    "error_type": exc.__class__.__name__,
                },
                status=500,
            )

        logger.info(
            "%s removido pelo aplicativo: user=%s media_type=%s instance_id=%s",
            title,
            user.id,
            media_type,
            instance_id,
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"{title} foi removido da sua biblioteca.",
                "deleted_id": instance_id,
                "media_type": media_type,
            },
            status=200,
        )

    data = _read_json_body(request)

    if data is None:
        return JsonResponse(
            {"detail": "Envie um JSON válido."},
            status=400,
        )

    status = data.get("status")

    if status not in Status.values:
        return JsonResponse(
            {
                "detail": "Status inválido.",
                "allowed_statuses": list(Status.values),
            },
            status=400,
        )

    try:
        media.status = status
        media.save()
        media.refresh_from_db()
    except Exception as exc:
        logger.exception(
            "Erro ao alterar status: user=%s media_type=%s instance_id=%s status=%s",
            user.id,
            media_type,
            instance_id,
            status,
        )
        return JsonResponse(
            {
                "detail": "Não foi possível alterar o status deste conteúdo.",
                "error_type": exc.__class__.__name__,
            },
            status=500,
        )

    logger.info(
        "Status atualizado pelo aplicativo: user=%s media_type=%s instance_id=%s status=%s",
        user.id,
        media_type,
        instance_id,
        status,
    )

    return JsonResponse(
        {
            "success": True,
            "message": "Status atualizado com sucesso.",
            "tracking": _serialize_tracking(
                media,
                media_type,
            ),
        },
        status=200,
    )
