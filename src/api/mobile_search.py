import logging

from django.apps import apps
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from api.views import (
    _api_jsonify,
    _get_authenticated_api_user,
)
from app.models import (
    MediaTypes,
    Status,
)
from app.providers import (
    services as provider_services,
)


logger = logging.getLogger(__name__)


STATUS_LABELS = {
    Status.COMPLETED.value: (
        "Concluído"
    ),
    Status.IN_PROGRESS.value: (
        "Em andamento"
    ),
    Status.PLANNING.value: (
        "Planejado"
    ),
    Status.PAUSED.value: (
        "Pausado"
    ),
    Status.DROPPED.value: (
        "Abandonado"
    ),
}


def login_not_required(view_func):
    view_func.login_required = False
    return view_func


def _get_authenticated_user(
    request,
):
    user = (
        _get_authenticated_api_user(
            request,
        )
    )

    if user is None:
        return None, JsonResponse(
            {
                "detail": (
                    "Autenticação "
                    "necessária."
                ),
            },
            status=401,
        )

    return user, None


def _status_label(
    media_type,
    status,
):
    if (
        media_type
        == MediaTypes.MOVIE.value
        and status
        == Status.COMPLETED.value
    ):
        return "Assistido"

    return STATUS_LABELS.get(
        status,
        status or "",
    )


def _iso_datetime(value):
    if value is None:
        return None

    if hasattr(
        value,
        "isoformat",
    ):
        return value.isoformat()

    return str(value)


def _get_provider_items(
    provider_payload,
):
    if isinstance(
        provider_payload,
        dict,
    ):
        items = (
            provider_payload.get(
                "results"
            )
        )

        if isinstance(
            items,
            list,
        ):
            return items

    if isinstance(
        provider_payload,
        list,
    ):
        return provider_payload

    return []


def _build_media_lookup(
    user,
    media_type,
    items,
):
    model = apps.get_model(
        app_label="app",
        model_name=media_type,
    )

    query = Q()

    valid_keys = set()

    for item in items:
        if not isinstance(
            item,
            dict,
        ):
            continue

        media_id = str(
            item.get(
                "media_id",
                "",
            )
        ).strip()

        source = str(
            item.get(
                "source",
                "",
            )
        ).strip()

        if (
            not media_id
            or not source
        ):
            continue

        key = (
            media_id,
            source,
        )

        valid_keys.add(key)

        query |= Q(
            item__media_id=media_id,
            item__source=source,
            item__media_type=(
                media_type
            ),
        )

    if (
        not valid_keys
        or not query
    ):
        return {}

    queryset = (
        model.objects
        .filter(
            query,
            user=user,
        )
        .select_related(
            "item"
        )
        .order_by(
            "-created_at",
            "-pk",
        )
    )

    lookup = {}

    for media in queryset:
        key = (
            str(
                media.item.media_id
            ),
            media.item.source,
        )

        if key not in lookup:
            lookup[key] = media

    return lookup


def _enrich_results(
    user,
    media_type,
    items,
):
    lookup = (
        _build_media_lookup(
            user,
            media_type,
            items,
        )
    )

    enriched = []

    for item in items:
        if not isinstance(
            item,
            dict,
        ):
            enriched.append(
                item
            )
            continue

        result = dict(item)

        media_id = str(
            result.get(
                "media_id",
                "",
            )
        ).strip()

        source = str(
            result.get(
                "source",
                "",
            )
        ).strip()

        media = lookup.get(
            (
                media_id,
                source,
            )
        )

        if media is None:
            result.update(
                {
                    "is_tracked": False,
                    "instance_id": None,
                    "status": None,
                    "status_label": None,
                    "watched": False,
                    "watched_at": None,
                }
            )

            enriched.append(
                result
            )
            continue

        status = getattr(
            media,
            "status",
            None,
        )

        watched = (
            media_type
            == MediaTypes.MOVIE.value
            and status
            == Status.COMPLETED.value
        )

        result.update(
            {
                "is_tracked": True,
                "instance_id": (
                    media.pk
                ),
                "status": status,
                "status_label": (
                    _status_label(
                        media_type,
                        status,
                    )
                ),
                "watched": watched,
                "watched_at": (
                    _iso_datetime(
                        getattr(
                            media,
                            "end_date",
                            None,
                        )
                    )
                    if watched
                    else None
                ),
            }
        )

        enriched.append(
            result
        )

    return enriched


@login_not_required
@require_GET
def mobile_search(
    request,
):
    user, error_response = (
        _get_authenticated_user(
            request,
        )
    )

    if error_response:
        return error_response

    query = (
        request.GET
        .get(
            "q",
            "",
        )
        .strip()
    )

    media_type = (
        request.GET
        .get(
            "type",
            request.GET.get(
                "media_type",
                "tv",
            ),
        )
        .strip()
    )

    source = (
        request.GET.get(
            "source"
        )
        or None
    )

    try:
        page = max(
            1,
            int(
                request.GET.get(
                    "page",
                    "1",
                )
            ),
        )
    except ValueError:
        page = 1

    if not query:
        return JsonResponse(
            {
                "detail": (
                    "Informe um termo "
                    "de busca."
                ),
                "results": [],
            },
            status=400,
        )

    if (
        media_type
        not in MediaTypes.values
    ):
        return JsonResponse(
            {
                "detail": (
                    "Tipo de mídia "
                    "inválido."
                ),
                "results": [],
            },
            status=400,
        )

    try:
        provider_payload = (
            provider_services.search(
                media_type=media_type,
                query=query,
                page=page,
                source=source,
            )
        )

        provider_items = (
            _get_provider_items(
                provider_payload
            )
        )

        enriched_items = (
            _enrich_results(
                user,
                media_type,
                provider_items,
            )
        )

        if isinstance(
            provider_payload,
            dict,
        ):
            result_payload = {
                **provider_payload,
                "results": (
                    enriched_items
                ),
            }
        else:
            result_payload = (
                enriched_items
            )

        return JsonResponse(
            {
                "query": query,
                "media_type": (
                    media_type
                ),
                "source": source,
                "page": page,
                "results": (
                    _api_jsonify(
                        result_payload
                    )
                ),
            },
            status=200,
        )

    except Exception as exc:
        logger.exception(
            (
                "Erro inesperado "
                "na busca mobile: "
                "media_type=%s "
                "query=%s"
            ),
            media_type,
            query,
        )

        return JsonResponse(
            {
                "detail": (
                    "Erro ao realizar "
                    "busca."
                ),
                "error_type": (
                    exc
                    .__class__
                    .__name__
                ),
                "error": str(
                    exc
                ),
                "results": [],
            },
            status=500,
        )