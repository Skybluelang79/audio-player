from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = (
            "id",
            "kind",
            "text",
            "url",
            "actor_id",
            "actor_name",
            "is_read",
            "read_at",
            "created_at",
        )
        read_only_fields = fields

    def get_actor_name(self, obj) -> str | None:
        if obj.actor_id is None:
            return None
        return obj.actor.display_name or obj.actor.email
