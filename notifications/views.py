"""Notification API: list, unread count, and read receipts."""

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """A listener's own notifications. Always scoped to ``request.user``."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = Notification.objects.filter(recipient=self.request.user).select_related(
            "actor"
        )
        unread = self.request.query_params.get("unread")
        if unread and unread.lower() in {"1", "true", "yes", "unread"}:
            queryset = queryset.filter(read_at__isnull=True)
        return queryset

    @extend_schema(
        tags=["notifications"],
        parameters=[
            OpenApiParameter(
                "unread",
                type=bool,
                description="When true, only return unread notifications.",
            )
        ],
        responses={200: NotificationSerializer(many=True)},
        summary="List notifications",
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=["notifications"],
        responses={200: None},
        summary="Unread notification count",
    )
    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        count = Notification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).count()
        return Response({"unread": count})

    @extend_schema(
        tags=["notifications"],
        request=None,
        responses={200: NotificationSerializer},
        summary="Mark one notification as read",
    )
    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()
        notification.mark_read()
        return Response(self.get_serializer(notification).data)

    @extend_schema(
        tags=["notifications"],
        request=None,
        responses={200: None},
        summary="Mark every notification as read",
    )
    @action(detail=False, methods=["post"])
    def read_all(self, request):
        updated = Notification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).update(read_at=timezone.now())
        return Response({"marked_read": updated})
