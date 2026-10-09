"""Transactional email helpers for account flows.

Kept separate from the views so the URL/token construction can be unit-tested
without going through HTTP, and so both the site and the API reuse one builder.
"""

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def make_uid(user) -> str:
    return urlsafe_base64_encode(force_bytes(user.pk))


def make_token(user) -> str:
    return default_token_generator.make_token(user)


def build_reset_link(user, request=None):
    """Return ``(url, uid, token)`` for a password reset.

    When ``PASSWORD_RESET_URL`` is configured the link points at that SPA route
    (with ``uid``/``token`` query params); otherwise it points at the
    server-rendered confirm page.
    """
    uid, token = make_uid(user), make_token(user)
    base = getattr(settings, "PASSWORD_RESET_URL", "")
    if base:
        return f"{base}?uid={uid}&token={token}", uid, token

    path = reverse(
        "accounts:password-reset-confirm", kwargs={"uidb64": uid, "token": token}
    )
    url = request.build_absolute_uri(path) if request is not None else path
    return url, uid, token


def send_password_reset_email(user, request=None):
    url, uid, token = build_reset_link(user, request)
    subject = "Reset your Auralis password"
    body = (
        f"Hi {user.display_name or user.email},\n\n"
        "We received a request to reset your Auralis password. Open the link "
        "below to choose a new one:\n\n"
        f"{url}\n\n"
        "If you use the API, POST the uid and token to "
        "/api/auth/password/reset/confirm/ with your new password:\n\n"
        f"  uid: {uid}\n  token: {token}\n\n"
        "If you did not request this, you can ignore this email.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email])
    return uid, token


def send_email_verification(user, request=None):
    uid, token = make_uid(user), make_token(user)
    subject = "Verify your Auralis email"
    body = (
        f"Hi {user.display_name or user.email},\n\n"
        "Confirm your email address by POSTing these values to "
        "/api/auth/email/verify/confirm/:\n\n"
        f"  uid: {uid}\n  token: {token}\n\n"
        "If you did not create an account, you can ignore this email.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email])
    return uid, token
