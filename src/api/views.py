import logging
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

from app.models import BasicMedia, MediaTypes, Status, Item
from app.providers import services as provider_services
from django.apps import apps
from django.db import IntegrityError
from lists.models import CustomList, CustomListItem



logger = logging.getLogger(__name__)
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





def _api_request_payload(request):
    """Read JSON or form payload for mobile API requests."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}"), None
        except json.JSONDecodeError:
            return None, JsonResponse(
                {
                    "detail": "JSON inválido.",
                },
                status=400,
            )

    return request.POST.dict(), None


def _api_jsonify(value):
    """Convert provider search responses to JSON-safe values."""
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _api_jsonify(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_api_jsonify(item) for item in value]

    if hasattr(value, "isoformat"):
        return value.isoformat()

    try:
        return float(value)
    except Exception:
        return str(value)





def _mobile_datetime(value):
    if not value:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _mobile_user_label(user):
    if not user:
        return ""

    full_name = ""

    try:
        full_name = user.get_full_name()
    except Exception:
        full_name = ""

    return full_name or getattr(user, "username", "") or getattr(user, "email", "")


def _serialize_custom_list(custom_list, include_preview=True):
    items_qs = CustomListItem.objects.filter(custom_list=custom_list).select_related("item").order_by("-date_added")

    preview = []

    if include_preview:
        for list_item in items_qs[:4]:
            item = list_item.item
            preview.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "image": item.image,
                    "media_id": item.media_id,
                    "media_type": item.media_type,
                    "source": item.source,
                    "season_number": item.season_number,
                    "episode_number": item.episode_number,
                    "date_added": _mobile_datetime(list_item.date_added),
                }
            )

    owner = getattr(custom_list, "owner", None)

    return {
        "id": custom_list.id,
        "name": custom_list.name,
        "description": getattr(custom_list, "description", "") or "",
        "owner": _mobile_user_label(owner),
        "item_count": items_qs.count(),
        "last_added": _mobile_datetime(CustomListItem.objects.get_last_added_date(custom_list)),
        "can_edit": False,
        "can_delete": False,
        "preview_items": preview,
    }


def _serialize_custom_list_item(list_item, user):
    item = list_item.item
    media_payload = None

    try:
        model = apps.get_model(app_label="app", model_name=item.media_type)
        media = model.objects.filter(item=item, user=user).first()

        if media:
            media_payload = _serialize_media(media)
    except Exception:
        media_payload = None

    return {
        "id": list_item.id,
        "date_added": _mobile_datetime(list_item.date_added),
        "item": {
            "id": item.id,
            "title": item.title,
            "image": item.image,
            "media_id": item.media_id,
            "media_type": item.media_type,
            "source": item.source,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
        },
        "media": media_payload,
    }


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def mobile_lists(request):
    """List or create custom lists for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        if request.method == "POST":
            payload, payload_error = _api_request_payload(request)

            if payload_error:
                return payload_error

            name = str(payload.get("name", "")).strip()
            description = str(payload.get("description", "")).strip()

            if not name:
                return JsonResponse(
                    {
                        "detail": "Informe o nome da lista.",
                    },
                    status=400,
                )

            custom_list = CustomList.objects.create(
                name=name,
                description=description,
                owner=user,
            )

            return JsonResponse(
                {
                    "created": True,
                    "list": _serialize_custom_list(custom_list),
                },
                status=201,
            )

        try:
            custom_lists = CustomList.objects.get_user_lists(user)
        except Exception:
            custom_lists = CustomList.objects.filter(owner=user)

        data = [
            _serialize_custom_list(custom_list)
            for custom_list in custom_lists
        ]

        return JsonResponse(
            {
                "count": len(data),
                "lists": data,
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_lists.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar listas.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_list_detail(request, list_id):
    """Return details and items from a custom list for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        try:
            custom_list = CustomList.objects.get(id=list_id)
        except CustomList.DoesNotExist:
            return JsonResponse(
                {
                    "detail": "Lista não encontrada.",
                },
                status=404,
            )

        try:
            can_view = custom_list.user_can_view(user)
        except Exception:
            can_view = custom_list.owner_id == user.id

        if not can_view:
            return JsonResponse(
                {
                    "detail": "Você não tem permissão para ver esta lista.",
                },
                status=403,
            )

        items = CustomListItem.objects.filter(
            custom_list=custom_list,
        ).select_related("item").order_by("-date_added")

        return JsonResponse(
            {
                "list": _serialize_custom_list(custom_list, include_preview=False),
                "items": [
                    _serialize_custom_list_item(list_item, user)
                    for list_item in items
                ],
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_list_detail.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar lista.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@csrf_exempt
@require_POST
def mobile_media_add(request):
    """Add a provider media item to the authenticated user's library."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        payload, payload_error = _api_request_payload(request)

        if payload_error:
            return payload_error

        media_id = str(payload.get("media_id", "")).strip()
        source = str(payload.get("source", "")).strip()
        media_type = str(payload.get("media_type", payload.get("type", ""))).strip()
        season_number = payload.get("season_number") or None
        status = payload.get("status") or Status.PLANNING.value

        if not media_id:
            return JsonResponse(
                {"detail": "Informe o media_id."},
                status=400,
            )

        if not source:
            return JsonResponse(
                {"detail": "Informe o source."},
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {"detail": "Tipo de mídia inválido."},
                status=400,
            )

        if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
            return JsonResponse(
                {
                    "detail": "Adicionar temporadas e episódios pelo app ainda não está disponível.",
                },
                status=400,
            )

        if status not in Status.values:
            status = Status.PLANNING.value

        try:
            metadata = provider_services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        except Exception:
            logger.exception(
                "Erro ao buscar metadata para adicionar mídia mobile: media_type=%s media_id=%s source=%s",
                media_type,
                media_id,
                source,
            )
            metadata = {
                "title": payload.get("title", ""),
                "image": payload.get("image", ""),
            }

        title = metadata.get("title") or payload.get("title") or ""
        image = metadata.get("image") or payload.get("image") or ""

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": title,
                "image": image,
            },
        )

        model = apps.get_model(app_label="app", model_name=media_type)

        existing_media = model.objects.filter(
            item=item,
            user=user,
        ).first()

        if existing_media:
            return JsonResponse(
                {
                    "created": False,
                    "already_exists": True,
                    "media": _serialize_media(existing_media),
                },
                status=200,
            )

        instance = model(
            item=item,
            user=user,
        )

        if hasattr(instance, "status"):
            instance.status = status

        if hasattr(instance, "score"):
            instance.score = None

        if hasattr(instance, "notes"):
            instance.notes = ""

        if hasattr(instance, "progress"):
            instance.progress = 0

        try:
            instance.save()
        except IntegrityError:
            existing_media = model.objects.filter(
                item=item,
                user=user,
            ).first()

            if existing_media:
                return JsonResponse(
                    {
                        "created": False,
                        "already_exists": True,
                        "media": _serialize_media(existing_media),
                    },
                    status=200,
                )

            raise

        logger.info(
            "%s added from mobile API by user %s.",
            instance,
            user.username,
        )

        return JsonResponse(
            {
                "created": True,
                "already_exists": False,
                "media": _serialize_media(instance),
            },
            status=201,
        )

    except Exception as exc:
        logger.exception("Erro inesperado ao adicionar mídia mobile.")

        return JsonResponse(
            {
                "detail": "Erro ao adicionar mídia.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_search(request):
    """Search media providers for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        query = request.GET.get("q", "").strip()
        media_type = request.GET.get("type", request.GET.get("media_type", "tv")).strip()
        source = request.GET.get("source") or None

        try:
            page = max(1, int(request.GET.get("page", "1")))
        except ValueError:
            page = 1

        if not query:
            return JsonResponse(
                {
                    "detail": "Informe um termo de busca.",
                    "results": [],
                },
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {
                    "detail": "Tipo de mídia inválido.",
                    "results": [],
                },
                status=400,
            )

        results = provider_services.search(
            media_type=media_type,
            query=query,
            page=page,
            source=source,
        )

        return JsonResponse(
            {
                "query": query,
                "media_type": media_type,
                "source": source,
                "page": page,
                "results": _api_jsonify(results),
            },
            status=200,
        )

    except Exception as exc:
        logger.exception(
            "Erro inesperado na busca mobile: media_type=%s query=%s",
            request.GET.get("type", request.GET.get("media_type", "tv")),
            request.GET.get("q", ""),
        )

        return JsonResponse(
            {
                "detail": "Erro ao realizar busca.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "results": [],
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_health(request):
    return JsonResponse(
        {
            "ok": True,
            "version": "mobile-detail-debug-2026-08-02",
        },
        status=200,
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





def _api_request_payload(request):
    """Read JSON or form payload for mobile API requests."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}"), None
        except json.JSONDecodeError:
            return None, JsonResponse(
                {
                    "detail": "JSON inválido.",
                },
                status=400,
            )

    return request.POST.dict(), None


def _api_jsonify(value):
    """Convert provider search responses to JSON-safe values."""
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _api_jsonify(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_api_jsonify(item) for item in value]

    if hasattr(value, "isoformat"):
        return value.isoformat()

    try:
        return float(value)
    except Exception:
        return str(value)





def _mobile_datetime(value):
    if not value:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _mobile_user_label(user):
    if not user:
        return ""

    full_name = ""

    try:
        full_name = user.get_full_name()
    except Exception:
        full_name = ""

    return full_name or getattr(user, "username", "") or getattr(user, "email", "")


def _serialize_custom_list(custom_list, include_preview=True):
    items_qs = CustomListItem.objects.filter(custom_list=custom_list).select_related("item").order_by("-date_added")

    preview = []

    if include_preview:
        for list_item in items_qs[:4]:
            item = list_item.item
            preview.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "image": item.image,
                    "media_id": item.media_id,
                    "media_type": item.media_type,
                    "source": item.source,
                    "season_number": item.season_number,
                    "episode_number": item.episode_number,
                    "date_added": _mobile_datetime(list_item.date_added),
                }
            )

    owner = getattr(custom_list, "owner", None)

    return {
        "id": custom_list.id,
        "name": custom_list.name,
        "description": getattr(custom_list, "description", "") or "",
        "owner": _mobile_user_label(owner),
        "item_count": items_qs.count(),
        "last_added": _mobile_datetime(CustomListItem.objects.get_last_added_date(custom_list)),
        "can_edit": False,
        "can_delete": False,
        "preview_items": preview,
    }


def _serialize_custom_list_item(list_item, user):
    item = list_item.item
    media_payload = None

    try:
        model = apps.get_model(app_label="app", model_name=item.media_type)
        media = model.objects.filter(item=item, user=user).first()

        if media:
            media_payload = _serialize_media(media)
    except Exception:
        media_payload = None

    return {
        "id": list_item.id,
        "date_added": _mobile_datetime(list_item.date_added),
        "item": {
            "id": item.id,
            "title": item.title,
            "image": item.image,
            "media_id": item.media_id,
            "media_type": item.media_type,
            "source": item.source,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
        },
        "media": media_payload,
    }


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def mobile_lists(request):
    """List or create custom lists for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        if request.method == "POST":
            payload, payload_error = _api_request_payload(request)

            if payload_error:
                return payload_error

            name = str(payload.get("name", "")).strip()
            description = str(payload.get("description", "")).strip()

            if not name:
                return JsonResponse(
                    {
                        "detail": "Informe o nome da lista.",
                    },
                    status=400,
                )

            custom_list = CustomList.objects.create(
                name=name,
                description=description,
                owner=user,
            )

            return JsonResponse(
                {
                    "created": True,
                    "list": _serialize_custom_list(custom_list),
                },
                status=201,
            )

        try:
            custom_lists = CustomList.objects.get_user_lists(user)
        except Exception:
            custom_lists = CustomList.objects.filter(owner=user)

        data = [
            _serialize_custom_list(custom_list)
            for custom_list in custom_lists
        ]

        return JsonResponse(
            {
                "count": len(data),
                "lists": data,
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_lists.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar listas.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_list_detail(request, list_id):
    """Return details and items from a custom list for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        try:
            custom_list = CustomList.objects.get(id=list_id)
        except CustomList.DoesNotExist:
            return JsonResponse(
                {
                    "detail": "Lista não encontrada.",
                },
                status=404,
            )

        try:
            can_view = custom_list.user_can_view(user)
        except Exception:
            can_view = custom_list.owner_id == user.id

        if not can_view:
            return JsonResponse(
                {
                    "detail": "Você não tem permissão para ver esta lista.",
                },
                status=403,
            )

        items = CustomListItem.objects.filter(
            custom_list=custom_list,
        ).select_related("item").order_by("-date_added")

        return JsonResponse(
            {
                "list": _serialize_custom_list(custom_list, include_preview=False),
                "items": [
                    _serialize_custom_list_item(list_item, user)
                    for list_item in items
                ],
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_list_detail.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar lista.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@csrf_exempt
@require_POST
def mobile_media_add(request):
    """Add a provider media item to the authenticated user's library."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        payload, payload_error = _api_request_payload(request)

        if payload_error:
            return payload_error

        media_id = str(payload.get("media_id", "")).strip()
        source = str(payload.get("source", "")).strip()
        media_type = str(payload.get("media_type", payload.get("type", ""))).strip()
        season_number = payload.get("season_number") or None
        status = payload.get("status") or Status.PLANNING.value

        if not media_id:
            return JsonResponse(
                {"detail": "Informe o media_id."},
                status=400,
            )

        if not source:
            return JsonResponse(
                {"detail": "Informe o source."},
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {"detail": "Tipo de mídia inválido."},
                status=400,
            )

        if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
            return JsonResponse(
                {
                    "detail": "Adicionar temporadas e episódios pelo app ainda não está disponível.",
                },
                status=400,
            )

        if status not in Status.values:
            status = Status.PLANNING.value

        try:
            metadata = provider_services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        except Exception:
            logger.exception(
                "Erro ao buscar metadata para adicionar mídia mobile: media_type=%s media_id=%s source=%s",
                media_type,
                media_id,
                source,
            )
            metadata = {
                "title": payload.get("title", ""),
                "image": payload.get("image", ""),
            }

        title = metadata.get("title") or payload.get("title") or ""
        image = metadata.get("image") or payload.get("image") or ""

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": title,
                "image": image,
            },
        )

        model = apps.get_model(app_label="app", model_name=media_type)

        existing_media = model.objects.filter(
            item=item,
            user=user,
        ).first()

        if existing_media:
            return JsonResponse(
                {
                    "created": False,
                    "already_exists": True,
                    "media": _serialize_media(existing_media),
                },
                status=200,
            )

        instance = model(
            item=item,
            user=user,
        )

        if hasattr(instance, "status"):
            instance.status = status

        if hasattr(instance, "score"):
            instance.score = None

        if hasattr(instance, "notes"):
            instance.notes = ""

        if hasattr(instance, "progress"):
            instance.progress = 0

        try:
            instance.save()
        except IntegrityError:
            existing_media = model.objects.filter(
                item=item,
                user=user,
            ).first()

            if existing_media:
                return JsonResponse(
                    {
                        "created": False,
                        "already_exists": True,
                        "media": _serialize_media(existing_media),
                    },
                    status=200,
                )

            raise

        logger.info(
            "%s added from mobile API by user %s.",
            instance,
            user.username,
        )

        return JsonResponse(
            {
                "created": True,
                "already_exists": False,
                "media": _serialize_media(instance),
            },
            status=201,
        )

    except Exception as exc:
        logger.exception("Erro inesperado ao adicionar mídia mobile.")

        return JsonResponse(
            {
                "detail": "Erro ao adicionar mídia.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_search(request):
    """Search media providers for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        query = request.GET.get("q", "").strip()
        media_type = request.GET.get("type", request.GET.get("media_type", "tv")).strip()
        source = request.GET.get("source") or None

        try:
            page = max(1, int(request.GET.get("page", "1")))
        except ValueError:
            page = 1

        if not query:
            return JsonResponse(
                {
                    "detail": "Informe um termo de busca.",
                    "results": [],
                },
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {
                    "detail": "Tipo de mídia inválido.",
                    "results": [],
                },
                status=400,
            )

        results = provider_services.search(
            media_type=media_type,
            query=query,
            page=page,
            source=source,
        )

        return JsonResponse(
            {
                "query": query,
                "media_type": media_type,
                "source": source,
                "page": page,
                "results": _api_jsonify(results),
            },
            status=200,
        )

    except Exception as exc:
        logger.exception(
            "Erro inesperado na busca mobile: media_type=%s query=%s",
            request.GET.get("type", request.GET.get("media_type", "tv")),
            request.GET.get("q", ""),
        )

        return JsonResponse(
            {
                "detail": "Erro ao realizar busca.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "results": [],
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_health(request):
    return JsonResponse(
        {
            "ok": True,
            "version": "mobile-detail-debug-2026-08-02",
        },
        status=200,
    )


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




def _api_safe_get(obj, attr, default=None):
    try:
        value = getattr(obj, attr)
    except Exception:
        return default

    if value is None:
        return default

    return value


def _api_json_value(value):
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if hasattr(value, "isoformat"):
        return value.isoformat()

    try:
        return float(value)
    except Exception:
        return str(value)


def _mobile_status_label(status, fallback=""):
    if status == Status.PLANNING.value or status == "Planning":
        return "Em breve"

    return STATUS_LABELS.get(status, fallback or status or "")


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
        "id": _api_json_value(_api_safe_get(media, "id")),
        "media_id": _api_json_value(_api_safe_get(media, "media_id", "")),
        "source": _api_json_value(_api_safe_get(media, "source", "")),
        "media_type": _api_json_value(media_type),
        "media_type_label": MEDIA_TYPE_LABELS.get(media_type, media_type),
        "title": _api_json_value(_api_safe_get(media, "title", "")),
        "image": _api_json_value(_api_safe_get(media, "image", "")),
        "season_number": _api_json_value(_api_safe_get(media, "season_number", None)),
        "episode_number": _api_json_value(_api_safe_get(media, "episode_number", None)),
        "status": _api_json_value(status),
        "status_label": _mobile_status_label(status),
        "score": _api_json_value(_api_safe_get(media, "score", None)),
        "progress": _api_json_value(_api_safe_get(media, "progress", None)),
        "formatted_progress": _api_json_value(_api_safe_get(media, "formatted_progress", "")),
        "max_progress": _api_json_value(_api_safe_get(media, "max_progress", None)),
        "progress_percent": _api_json_value(_api_safe_get(media, "progress_percent", None)),
        "start_date": _api_json_value(_api_safe_get(media, "start_date", None)),
        "end_date": _api_json_value(_api_safe_get(media, "end_date", None)),
        "progressed_at": _api_json_value(_api_safe_get(media, "progressed_at", None)),
        "last_watched": _api_json_value(_api_safe_get(media, "last_watched", "")),
        "next_episode_number": _api_json_value(_api_safe_get(media, "next_episode_number", None)),
        "next_episode_title": _api_json_value(_api_safe_get(media, "next_episode_title", "")),
        "next_event": serialized_next_event,
    }





def _api_request_payload(request):
    """Read JSON or form payload for mobile API requests."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            return json.loads(request.body.decode("utf-8") or "{}"), None
        except json.JSONDecodeError:
            return None, JsonResponse(
                {
                    "detail": "JSON inválido.",
                },
                status=400,
            )

    return request.POST.dict(), None


def _api_jsonify(value):
    """Convert provider search responses to JSON-safe values."""
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _api_jsonify(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_api_jsonify(item) for item in value]

    if hasattr(value, "isoformat"):
        return value.isoformat()

    try:
        return float(value)
    except Exception:
        return str(value)





def _mobile_datetime(value):
    if not value:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _mobile_user_label(user):
    if not user:
        return ""

    full_name = ""

    try:
        full_name = user.get_full_name()
    except Exception:
        full_name = ""

    return full_name or getattr(user, "username", "") or getattr(user, "email", "")


def _serialize_custom_list(custom_list, include_preview=True):
    items_qs = CustomListItem.objects.filter(custom_list=custom_list).select_related("item").order_by("-date_added")

    preview = []

    if include_preview:
        for list_item in items_qs[:4]:
            item = list_item.item
            preview.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "image": item.image,
                    "media_id": item.media_id,
                    "media_type": item.media_type,
                    "source": item.source,
                    "season_number": item.season_number,
                    "episode_number": item.episode_number,
                    "date_added": _mobile_datetime(list_item.date_added),
                }
            )

    owner = getattr(custom_list, "owner", None)

    return {
        "id": custom_list.id,
        "name": custom_list.name,
        "description": getattr(custom_list, "description", "") or "",
        "owner": _mobile_user_label(owner),
        "item_count": items_qs.count(),
        "last_added": _mobile_datetime(CustomListItem.objects.get_last_added_date(custom_list)),
        "can_edit": False,
        "can_delete": False,
        "preview_items": preview,
    }


def _serialize_custom_list_item(list_item, user):
    item = list_item.item
    media_payload = None

    try:
        model = apps.get_model(app_label="app", model_name=item.media_type)
        media = model.objects.filter(item=item, user=user).first()

        if media:
            media_payload = _serialize_media(media)
    except Exception:
        media_payload = None

    return {
        "id": list_item.id,
        "date_added": _mobile_datetime(list_item.date_added),
        "item": {
            "id": item.id,
            "title": item.title,
            "image": item.image,
            "media_id": item.media_id,
            "media_type": item.media_type,
            "source": item.source,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
        },
        "media": media_payload,
    }


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def mobile_lists(request):
    """List or create custom lists for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        if request.method == "POST":
            payload, payload_error = _api_request_payload(request)

            if payload_error:
                return payload_error

            name = str(payload.get("name", "")).strip()
            description = str(payload.get("description", "")).strip()

            if not name:
                return JsonResponse(
                    {
                        "detail": "Informe o nome da lista.",
                    },
                    status=400,
                )

            custom_list = CustomList.objects.create(
                name=name,
                description=description,
                owner=user,
            )

            return JsonResponse(
                {
                    "created": True,
                    "list": _serialize_custom_list(custom_list),
                },
                status=201,
            )

        try:
            custom_lists = CustomList.objects.get_user_lists(user)
        except Exception:
            custom_lists = CustomList.objects.filter(owner=user)

        data = [
            _serialize_custom_list(custom_list)
            for custom_list in custom_lists
        ]

        return JsonResponse(
            {
                "count": len(data),
                "lists": data,
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_lists.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar listas.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_list_detail(request, list_id):
    """Return details and items from a custom list for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        try:
            custom_list = CustomList.objects.get(id=list_id)
        except CustomList.DoesNotExist:
            return JsonResponse(
                {
                    "detail": "Lista não encontrada.",
                },
                status=404,
            )

        try:
            can_view = custom_list.user_can_view(user)
        except Exception:
            can_view = custom_list.owner_id == user.id

        if not can_view:
            return JsonResponse(
                {
                    "detail": "Você não tem permissão para ver esta lista.",
                },
                status=403,
            )

        items = CustomListItem.objects.filter(
            custom_list=custom_list,
        ).select_related("item").order_by("-date_added")

        return JsonResponse(
            {
                "list": _serialize_custom_list(custom_list, include_preview=False),
                "items": [
                    _serialize_custom_list_item(list_item, user)
                    for list_item in items
                ],
            },
            status=200,
        )

    except Exception as exc:
        logger.exception("Erro inesperado em mobile_list_detail.")

        return JsonResponse(
            {
                "detail": "Erro ao carregar lista.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@csrf_exempt
@require_POST
def mobile_media_add(request):
    """Add a provider media item to the authenticated user's library."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

        payload, payload_error = _api_request_payload(request)

        if payload_error:
            return payload_error

        media_id = str(payload.get("media_id", "")).strip()
        source = str(payload.get("source", "")).strip()
        media_type = str(payload.get("media_type", payload.get("type", ""))).strip()
        season_number = payload.get("season_number") or None
        status = payload.get("status") or Status.PLANNING.value

        if not media_id:
            return JsonResponse(
                {"detail": "Informe o media_id."},
                status=400,
            )

        if not source:
            return JsonResponse(
                {"detail": "Informe o source."},
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {"detail": "Tipo de mídia inválido."},
                status=400,
            )

        if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
            return JsonResponse(
                {
                    "detail": "Adicionar temporadas e episódios pelo app ainda não está disponível.",
                },
                status=400,
            )

        if status not in Status.values:
            status = Status.PLANNING.value

        try:
            metadata = provider_services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        except Exception:
            logger.exception(
                "Erro ao buscar metadata para adicionar mídia mobile: media_type=%s media_id=%s source=%s",
                media_type,
                media_id,
                source,
            )
            metadata = {
                "title": payload.get("title", ""),
                "image": payload.get("image", ""),
            }

        title = metadata.get("title") or payload.get("title") or ""
        image = metadata.get("image") or payload.get("image") or ""

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": title,
                "image": image,
            },
        )

        model = apps.get_model(app_label="app", model_name=media_type)

        existing_media = model.objects.filter(
            item=item,
            user=user,
        ).first()

        if existing_media:
            return JsonResponse(
                {
                    "created": False,
                    "already_exists": True,
                    "media": _serialize_media(existing_media),
                },
                status=200,
            )

        instance = model(
            item=item,
            user=user,
        )

        if hasattr(instance, "status"):
            instance.status = status

        if hasattr(instance, "score"):
            instance.score = None

        if hasattr(instance, "notes"):
            instance.notes = ""

        if hasattr(instance, "progress"):
            instance.progress = 0

        try:
            instance.save()
        except IntegrityError:
            existing_media = model.objects.filter(
                item=item,
                user=user,
            ).first()

            if existing_media:
                return JsonResponse(
                    {
                        "created": False,
                        "already_exists": True,
                        "media": _serialize_media(existing_media),
                    },
                    status=200,
                )

            raise

        logger.info(
            "%s added from mobile API by user %s.",
            instance,
            user.username,
        )

        return JsonResponse(
            {
                "created": True,
                "already_exists": False,
                "media": _serialize_media(instance),
            },
            status=201,
        )

    except Exception as exc:
        logger.exception("Erro inesperado ao adicionar mídia mobile.")

        return JsonResponse(
            {
                "detail": "Erro ao adicionar mídia.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_search(request):
    """Search media providers for the mobile app."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        query = request.GET.get("q", "").strip()
        media_type = request.GET.get("type", request.GET.get("media_type", "tv")).strip()
        source = request.GET.get("source") or None

        try:
            page = max(1, int(request.GET.get("page", "1")))
        except ValueError:
            page = 1

        if not query:
            return JsonResponse(
                {
                    "detail": "Informe um termo de busca.",
                    "results": [],
                },
                status=400,
            )

        if media_type not in MediaTypes.values:
            return JsonResponse(
                {
                    "detail": "Tipo de mídia inválido.",
                    "results": [],
                },
                status=400,
            )

        results = provider_services.search(
            media_type=media_type,
            query=query,
            page=page,
            source=source,
        )

        return JsonResponse(
            {
                "query": query,
                "media_type": media_type,
                "source": source,
                "page": page,
                "results": _api_jsonify(results),
            },
            status=200,
        )

    except Exception as exc:
        logger.exception(
            "Erro inesperado na busca mobile: media_type=%s query=%s",
            request.GET.get("type", request.GET.get("media_type", "tv")),
            request.GET.get("q", ""),
        )

        return JsonResponse(
            {
                "detail": "Erro ao realizar busca.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "results": [],
            },
            status=500,
        )


@login_not_required
@require_GET
def mobile_health(request):
    return JsonResponse(
        {
            "ok": True,
            "version": "mobile-detail-debug-2026-08-02",
        },
        status=200,
    )


@login_not_required
@require_GET
def media_detail(request, media_type, instance_id):
    """Return details for a single media item."""
    try:
        auth_result = _get_authenticated_api_user(request)

        if isinstance(auth_result, JsonResponse):
            return auth_result

        user = auth_result

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
        except Exception as exc:
            error_name = exc.__class__.__name__

            if "DoesNotExist" in error_name:
                return JsonResponse(
                    {"detail": "Mídia não encontrada."},
                    status=404,
                )

            logger.exception(
                "Erro ao buscar detalhe da mídia mobile: media_type=%s instance_id=%s user=%s",
                media_type,
                instance_id,
                user.id,
            )

            return JsonResponse(
                {
                    "detail": "Erro ao buscar mídia.",
                    "error_type": error_name,
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

    except Exception as exc:
        logger.exception(
            "Erro inesperado no endpoint media_detail mobile: media_type=%s instance_id=%s",
            media_type,
            instance_id,
        )

        return JsonResponse(
            {
                "detail": "Erro inesperado no detalhe da mídia.",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            status=500,
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
