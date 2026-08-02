import json

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth import authenticate, get_user_model, login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from app.models import BasicMedia, MediaTypes, Status


MOBILE_TOKEN_SALT = "flexihub.mobile.auth"
MOBILE_TOKEN_MAX_AGE = getattr(
    settings,
    "MOBILE_API_TOKEN_MAX_AGE",
    60 * 60 * 24 * 60,
)


MEDIA_TYPE_LABELS = {
    MediaTypes.TV.value: "Série",
    MediaTypes.SEASON.value: "Temporada",
    MediaTypes.EPISODE.value: "Episódio",
    MediaTypes.MOVIE.value: "Filme",
    MediaTypes.ANIME.value: "Anime",
    MediaTypes.MANGA.value: "Mangá",
    MediaTypes.GAME.value: "Jogo",
    MediaTypes.BOOK.value: "Livro",
    MediaTypes.COMIC.value: "Quadrinho",
    MediaTypes.BOARDGAME.value: "Jogo de tabuleiro",
}


STATUS_LABELS = {
    Status.COMPLETED.value: "Concluído",
    Status.IN_PROGRESS.value: "Em andamento",
    Status.PLANNING.value: "Planejado",
    Status.PAUSED.value: "Pausado",
    Status.DROPPED.value: "Abandonado",
}


def _json_response(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _error(message, status=400):
    return _json_response({"detail": message}, status=status)


def _read_json_body(request):
    try:
        raw_body = request.body.decode("utf-8") if request.body else "{}"
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return None


def _iso_datetime(value):
    if value is None:
        return None

    return value.isoformat()


def _safe_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_decimal(value):
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _progress_percent(progress, max_progress):
    progress_value = _safe_decimal(progress)
    max_progress_value = _safe_decimal(max_progress)

    if progress_value is None or not max_progress_value or max_progress_value <= 0:
        return None

    return round(min((progress_value / max_progress_value) * 100, 100), 1)


def _user_payload(user):
    full_name = user.get_full_name()

    return {
        "id": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "name": full_name or user.get_username(),
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _create_mobile_token(user):
    payload = {
        "user_id": user.pk,
        "auth_hash": user.get_session_auth_hash(),
        "issued_at": int(timezone.now().timestamp()),
    }

    return signing.dumps(payload, salt=MOBILE_TOKEN_SALT)


def _get_bearer_token(request):
    authorization = request.headers.get("Authorization", "")

    if not authorization.startswith("Bearer "):
        return ""

    return authorization.removeprefix("Bearer ").strip()


def _get_user_from_token(request):
    token = _get_bearer_token(request)

    if not token:
        return None

    try:
        payload = signing.loads(
            token,
            salt=MOBILE_TOKEN_SALT,
            max_age=MOBILE_TOKEN_MAX_AGE,
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None

    user_id = payload.get("user_id")
    auth_hash = payload.get("auth_hash")

    if not user_id or not auth_hash:
        return None

    User = get_user_model()

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return None

    if user.get_session_auth_hash() != auth_hash:
        return None

    return user


def _get_authenticated_api_user(request):
    user = _get_user_from_token(request)

    if user is None and request.user.is_authenticated:
        user = request.user

    return user


def _authenticate_with_username_or_email(identifier, password):
    user = authenticate(username=identifier, password=password)

    if user is not None:
        return user

    User = get_user_model()

    try:
        matched_user = User.objects.get(email__iexact=identifier)
    except User.DoesNotExist:
        return None
    except User.MultipleObjectsReturned:
        return None

    return authenticate(username=matched_user.get_username(), password=password)


def _serialize_event(event):
    if event is None:
        return None

    return {
        "id": event.pk,
        "title": str(event),
        "content_number": event.content_number,
        "readable_content_number": event.readable_content_number,
        "datetime": _iso_datetime(event.datetime),
    }


def _serialize_media(media):
    item = media.item
    max_progress = getattr(media, "max_progress", None)
    progress = getattr(media, "progress", None)

    return {
        "id": media.pk,
        "media_id": item.media_id,
        "source": item.source,
        "media_type": item.media_type,
        "media_type_label": MEDIA_TYPE_LABELS.get(item.media_type, item.media_type),
        "title": item.title,
        "image": item.image,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "status": media.status,
        "status_label": STATUS_LABELS.get(media.status, media.status),
        "score": _safe_decimal(getattr(media, "score", None)),
        "progress": _safe_int(progress),
        "formatted_progress": getattr(media, "formatted_progress", str(progress or 0)),
        "max_progress": _safe_int(max_progress),
        "progress_percent": _progress_percent(progress, max_progress),
        "start_date": _iso_datetime(getattr(media, "start_date", None)),
        "end_date": _iso_datetime(getattr(media, "end_date", None)),
        "progressed_at": _iso_datetime(getattr(media, "progressed_at", None)),
        "last_watched": getattr(media, "last_watched", ""),
        "next_episode_number": getattr(media, "next_episode_number", None),
        "next_episode_title": getattr(media, "next_episode_title", ""),
        "next_event": _serialize_event(getattr(media, "next_event", None)),
    }


@login_not_required
@csrf_exempt
@require_POST
def login(request):
    data = _read_json_body(request)

    if data is None:
        return _error("Envie um JSON válido.", status=400)

    identifier = str(
        data.get("username")
        or data.get("email")
        or data.get("login")
        or ""
    ).strip()
    password = str(data.get("password") or "")

    if not identifier or not password:
        return _error("Informe usuário/e-mail e senha.", status=400)

    user = _authenticate_with_username_or_email(identifier, password)

    if user is None:
        return _error("Usuário/e-mail ou senha inválidos.", status=401)

    if not user.is_active:
        return _error("Esta conta está inativa.", status=403)

    django_login(request, user)

    token = _create_mobile_token(user)

    return _json_response(
        {
            "token": token,
            "access": token,
            "user": _user_payload(user),
        }
    )


@login_not_required
@require_GET
def me(request):
    user = _get_authenticated_api_user(request)

    if user is None:
        return _error("Autenticação necessária.", status=401)

    return _json_response({"user": _user_payload(user)})


@login_not_required
@csrf_exempt
@require_POST
def logout(request):
    django_logout(request)

    return _json_response({"ok": True})


@login_not_required
@require_GET
def home_next_up(request):
    user = _get_authenticated_api_user(request)

    if user is None:
        return _error("Autenticação necessária.", status=401)

    try:
        items_limit = int(request.GET.get("limit", 8))
    except ValueError:
        items_limit = 8

    items_limit = max(1, min(items_limit, 20))

    sort_by = getattr(user, "home_sort", "upcoming") or "upcoming"

    sections = []

    for status in (Status.IN_PROGRESS.value, Status.PLANNING.value):
        media_types = BasicMedia.objects.get_home_status(
            user=user,
            status=status,
            sort_by=sort_by,
            items_limit=items_limit,
        )

        serialized_media_types = []

        for media_type, media_payload in media_types.items():
            items = media_payload.get("items", [])
            total = media_payload.get("total", 0)

            serialized_media_types.append(
                {
                    "media_type": media_type,
                    "media_type_label": MEDIA_TYPE_LABELS.get(media_type, media_type),
                    "total": total,
                    "items": [_serialize_media(media) for media in items],
                }
            )

        sections.append(
            {
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "count": sum(media_type["total"] for media_type in serialized_media_types),
                "media_types": serialized_media_types,
            }
        )

    return _json_response(
        {
            "user": _user_payload(user),
            "sort": sort_by,
            "limit": items_limit,
            "sections": sections,
        }
    )





def _api_safe_get(obj, attr, default=None):
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _api_iso(value):
    if value is None:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def _serialize_media_detail(media):
    """Serialize a single media item safely for the mobile detail screen."""
    media_type = _api_safe_get(media, "media_type", "")
    status = _api_safe_get(media, "status", "")

    next_event = _api_safe_get(media, "next_event", None)

    try:
        serialized_next_event = _serialize_event(next_event) if next_event else None
    except Exception:
        serialized_next_event = None

    return {
        "id": _api_safe_get(media, "id"),
        "media_id": _api_safe_get(media, "media_id", ""),
        "source": _api_safe_get(media, "source", ""),
        "media_type": media_type,
        "media_type_label": MEDIA_TYPE_LABELS.get(media_type, media_type),
        "title": _api_safe_get(media, "title", ""),
        "image": _api_safe_get(media, "image", ""),
        "season_number": _api_safe_get(media, "season_number", None),
        "episode_number": _api_safe_get(media, "episode_number", None),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "score": _api_safe_get(media, "score", None),
        "progress": _api_safe_get(media, "progress", None),
        "formatted_progress": str(_api_safe_get(media, "formatted_progress", "")),
        "max_progress": _api_safe_get(media, "max_progress", None),
        "progress_percent": _api_safe_get(media, "progress_percent", None),
        "start_date": _api_iso(_api_safe_get(media, "start_date", None)),
        "end_date": _api_iso(_api_safe_get(media, "end_date", None)),
        "progressed_at": _api_iso(_api_safe_get(media, "progressed_at", None)),
        "last_watched": str(_api_safe_get(media, "last_watched", "")),
        "next_episode_number": _api_safe_get(media, "next_episode_number", None),
        "next_episode_title": _api_safe_get(media, "next_episode_title", ""),
        "next_event": serialized_next_event,
        "notes": _api_safe_get(media, "notes", ""),
        "description": (
            _api_safe_get(media, "description", "")
            or _api_safe_get(media, "synopsis", "")
            or _api_safe_get(media, "overview", "")
        ),
        "genres": _api_safe_get(media, "genres", []) or [],
    }


@login_not_required
@require_GET
def media_detail(request, media_type, instance_id):
    """Return details for a single media item."""
    user, error_response = _get_authenticated_api_user(request)

    if error_response:
        return error_response

    if media_type not in MediaTypes.values:
        return JsonResponse(
            {"detail": "Tipo de mídia inválido."},
            status=400,
        )

    try:
        media = BasicMedia.objects.get_media_prefetch(
            user=user,
            media_type=media_type,
            instance_id=instance_id,
        )
    except ObjectDoesNotExist:
        return JsonResponse(
            {"detail": "Mídia não encontrada."},
            status=404,
        )
    except Exception as exc:
        logger.exception(
            "Erro ao buscar detalhe da mídia mobile: media_type=%s instance_id=%s user=%s",
            media_type,
            instance_id,
            user.id,
        )
        return JsonResponse(
            {
                "detail": "Erro ao buscar mídia.",
                "error": str(exc),
            },
            status=500,
        )

    try:
        media_payload = _serialize_media(media)
    except Exception:
        logger.exception(
            "Erro ao serializar mídia com _serialize_media. Usando serialização segura: media_type=%s instance_id=%s user=%s",
            media_type,
            instance_id,
            user.id,
        )
        media_payload = _serialize_media_detail(media)

    return JsonResponse(
        {
            "media": media_payload,
        },
        status=200,
    )


@login_not_required
@csrf_exempt
@require_POST
def media_progress(request, media_type, instance_id):
    user = _get_authenticated_api_user(request)

    if user is None:
        return _error("Autenticação necessária.", status=401)

    if media_type not in MediaTypes.values:
        return _error("Tipo de mídia inválido.", status=400)

    data = _read_json_body(request)

    if data is None:
        return _error("Envie um JSON válido.", status=400)

    operation = str(data.get("operation") or "increase").strip()

    if operation not in {"increase", "decrease"}:
        return _error("Operação inválida.", status=400)

    try:
        media = BasicMedia.objects.get_media_prefetch(
            user=user,
            media_type=media_type,
            instance_id=instance_id,
        )
    except ObjectDoesNotExist:
        return _error("Mídia não encontrada.", status=404)

    try:
        if operation == "increase":
            media.increase_progress()
        else:
            media.decrease_progress()
    except Exception:
        return _error("Não foi possível atualizar o progresso desta mídia.", status=400)

    try:
        updated_media = BasicMedia.objects.get_media_prefetch(
            user=user,
            media_type=media_type,
            instance_id=instance_id,
        )
    except ObjectDoesNotExist:
        updated_media = media

    return _json_response(
        {
            "media": _serialize_media(updated_media),
        }
    )
