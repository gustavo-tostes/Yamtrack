from allauth.account.decorators import login_not_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from api.views import _get_authenticated_api_user
from lists.models import CustomList


def _serialize_user(user):
    return {
        "id": user.id,
        "username": user.username,
        "name": getattr(user, "name", "") or user.get_full_name() or user.username,
        "email": user.email,
    }


def _serialize_item_preview(item):
    return {
        "id": item.id,
        "media_id": str(item.media_id),
        "source": item.source,
        "media_type": item.media_type,
        "title": item.title,
        "image": item.image,
        "season_number": getattr(item, "season_number", None),
    }


def _serialize_custom_list(custom_list, user):
    items = list(custom_list.items.all())
    collaborators = list(custom_list.collaborators.all())
    list_entries = list(custom_list.customlistitem_set.all())

    last_entry = list_entries[0] if list_entries else None

    return {
        "id": custom_list.id,
        "name": custom_list.name,
        "description": custom_list.description,
        "image": custom_list.image,
        "item_count": len(items),
        "last_added_at": last_entry.date_added.isoformat() if last_entry else None,
        "owner": _serialize_user(custom_list.owner),
        "is_owner": custom_list.owner_id == user.id,
        "can_edit": custom_list.user_can_edit(user),
        "can_delete": custom_list.user_can_delete(user),
        "collaborators": [_serialize_user(collaborator) for collaborator in collaborators],
        "preview_items": [_serialize_item_preview(item) for item in items[:4]],
    }


@login_not_required
@require_GET
def mobile_lists(request):
    auth_result = _get_authenticated_api_user(request)

    if isinstance(auth_result, JsonResponse):
        return auth_result

    user = auth_result

    custom_lists = CustomList.objects.get_user_lists(user).order_by("name")
    serialized_lists = [
        _serialize_custom_list(custom_list, user)
        for custom_list in custom_lists
    ]

    return JsonResponse(
        {
            "count": len(serialized_lists),
            "results": serialized_lists,
        },
        status=200,
    )
