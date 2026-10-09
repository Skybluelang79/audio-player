from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "recipient", "actor", "is_read", "created_at")
    list_filter = ("kind", "read_at")
    search_fields = ("text", "recipient__email", "actor__email")
    autocomplete_fields = ("recipient", "actor")
    date_hierarchy = "created_at"
