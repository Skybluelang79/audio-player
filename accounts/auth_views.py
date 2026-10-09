"""Server-rendered password-reset pages.

Built on Django's own views so token generation, expiry and one-time semantics
are the framework's. Only templates, email copy and success URLs are customised.
The JSON equivalents live in ``accounts.views`` for API clients.
"""

from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy


class ResetRequestView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/email/password_reset_email.txt"
    subject_template_name = "accounts/email/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password-reset-done")


class ResetRequestDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class ResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password-reset-complete")


class ResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"
