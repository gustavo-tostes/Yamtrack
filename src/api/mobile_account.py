from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from api.views import (
    _create_mobile_token,
    _get_authenticated_api_user,
    _read_json_body,
    _user_payload,
)
from users.forms import PasswordChangeForm


def login_not_required(view_func):
    """Mark this API view as public for login-required middleware.

    Authentication is still handled manually through the Authorization header.
    """
    view_func.login_required = False
    return view_func


def _get_authenticated_user(request):
    user = _get_authenticated_api_user(request)

    if user is None:
        return None, JsonResponse(
            {
                "detail": "Autenticação necessária.",
            },
            status=401,
        )

    return user, None


def _account_payload(user):
    payload = _user_payload(user)

    payload.update(
        {
            "profile_private": user.profile_private,
            "is_demo": user.is_demo,
        }
    )

    return payload


def _serialize_form_errors(form):
    errors = {}

    for field, field_errors in form.errors.items():
        errors[field] = [
            str(error)
            for error in field_errors
        ]

    return errors


@login_not_required
@require_GET
def mobile_account(request):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    return JsonResponse(
        {
            "user": _account_payload(user),
        },
        status=200,
    )


@login_not_required
@csrf_exempt
@require_POST
def mobile_profile_privacy(request):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    if user.is_demo:
        return JsonResponse(
            {
                "detail": (
                    "Esta configuração não pode ser alterada "
                    "na conta de demonstração."
                ),
            },
            status=403,
        )

    data = _read_json_body(request)

    if data is None:
        return JsonResponse(
            {
                "detail": "Envie um JSON válido.",
            },
            status=400,
        )

    if "profile_private" not in data:
        return JsonResponse(
            {
                "detail": (
                    "Informe o valor de profile_private."
                ),
            },
            status=400,
        )

    profile_private = data.get("profile_private")

    if not isinstance(profile_private, bool):
        return JsonResponse(
            {
                "detail": (
                    "profile_private deve ser true ou false."
                ),
            },
            status=400,
        )

    user.profile_private = profile_private
    user.save(
        update_fields=[
            "profile_private",
        ]
    )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Perfil privado ativado."
                if profile_private
                else "Perfil privado desativado."
            ),
            "user": _account_payload(user),
        },
        status=200,
    )


@login_not_required
@csrf_exempt
@require_POST
def mobile_change_password(request):
    user, error_response = _get_authenticated_user(request)

    if error_response:
        return error_response

    if user.is_demo:
        return JsonResponse(
            {
                "detail": (
                    "A senha da conta de demonstração "
                    "não pode ser alterada."
                ),
            },
            status=403,
        )

    data = _read_json_body(request)

    if data is None:
        return JsonResponse(
            {
                "detail": "Envie um JSON válido.",
            },
            status=400,
        )

    old_password = str(
        data.get("old_password") or ""
    )

    new_password1 = str(
        data.get("new_password1")
        or data.get("new_password")
        or ""
    )

    new_password2 = str(
        data.get("new_password2")
        or data.get("confirm_password")
        or ""
    )

    if not old_password:
        return JsonResponse(
            {
                "detail": (
                    "Informe sua senha atual."
                ),
            },
            status=400,
        )

    if not new_password1:
        return JsonResponse(
            {
                "detail": (
                    "Informe a nova senha."
                ),
            },
            status=400,
        )

    if not new_password2:
        return JsonResponse(
            {
                "detail": (
                    "Confirme a nova senha."
                ),
            },
            status=400,
        )

    password_form = PasswordChangeForm(
        user=user,
        data={
            "old_password": old_password,
            "new_password1": new_password1,
            "new_password2": new_password2,
        },
    )

    if not password_form.is_valid():
        errors = _serialize_form_errors(
            password_form
        )

        first_error = None

        for field_errors in errors.values():
            if field_errors:
                first_error = field_errors[0]
                break

        return JsonResponse(
            {
                "success": False,
                "detail": (
                    first_error
                    or "Não foi possível alterar a senha."
                ),
                "errors": errors,
            },
            status=400,
        )

    user = password_form.save()

    new_token = _create_mobile_token(user)

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Senha alterada com sucesso."
            ),
            "token": new_token,
            "access": new_token,
            "user": _account_payload(user),
        },
        status=200,
    )