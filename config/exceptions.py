"""Uniform API error envelope.

All failures share one shape so clients can parse errors generically:

    {"error": {"status": 400, "code": "validation_error", "details": {...}}}
"""

import logging

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

_CODE_BY_STATUS = {
    status.HTTP_400_BAD_REQUEST: "validation_error",
    status.HTTP_401_UNAUTHORIZED: "not_authenticated",
    status.HTTP_403_FORBIDDEN: "permission_denied",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_406_NOT_ACCEPTABLE: "not_acceptable",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_429_TOO_MANY_REQUESTS: "throttled",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "server_error",
}


def api_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        exc = _as_drf_error(exc)
    if isinstance(exc, Http404):
        exc = _NotFound(str(exc))
    if isinstance(exc, PermissionDenied):
        exc = _Forbidden(str(exc))

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API exception", exc_info=exc)
        return Response(
            {
                "error": {
                    "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "code": "server_error",
                    "detail": "An unexpected error occurred.",
                }
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code = _CODE_BY_STATUS.get(response.status_code, "error")
    payload = {
        "error": {
            "status": response.status_code,
            "code": code,
            "detail": _first_message(response.data),
            "details": response.data,
        }
    }
    response.data = payload
    return response


def _first_message(data):
    """Pull a human readable message out of a possibly nested DRF error body."""
    if isinstance(data, dict):
        for key in ("detail", "non_field_errors"):
            if key in data:
                return _first_message(data[key])
        for value in data.values():
            message = _first_message(value)
            if message:
                return message
        return None
    if isinstance(data, (list, tuple)):
        for item in data:
            message = _first_message(item)
            if message:
                return message
        return None
    return str(data) if data is not None else None


def _as_drf_error(exc):
    from rest_framework.exceptions import ValidationError as DRFValidationError

    if hasattr(exc, "message_dict"):
        return DRFValidationError(exc.message_dict)
    return DRFValidationError(getattr(exc, "messages", [str(exc)]))


def _NotFound(detail):
    from rest_framework.exceptions import NotFound

    return NotFound(detail or "Not found.")


def _Forbidden(detail):
    from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

    return DRFPermissionDenied(detail or "Permission denied.")
