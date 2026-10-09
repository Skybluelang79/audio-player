from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"

    def ready(self):
        # Import for the side effect of connecting the signal receivers.
        from . import signals  # noqa: F401
