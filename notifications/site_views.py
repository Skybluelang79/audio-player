"""Listener-facing notification inbox."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from music.site_views import SiteViewMixin

from .models import Notification


class NotificationListView(LoginRequiredMixin, SiteViewMixin, ListView):
    template_name = "notifications/list.html"
    nav_active = "notifications"
    context_object_name = "notifications"
    paginate_by = 25

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user).select_related(
            "actor"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unread_count"] = Notification.objects.filter(
            recipient=self.request.user, read_at__isnull=True
        ).count()
        return context
