"""User admin: role management, verification and quick catalogue access."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.html import format_html

from .models import User


class AudioUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "display_name", "role")


class AudioUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = AudioUserCreationForm
    form = AudioUserChangeForm
    model = User

    list_display = (
        "email", "display_name", "role_badge", "is_verified", "is_staff",
        "is_active", "last_login", "joined",
    )
    list_display_links = ("email",)
    list_filter = ("role", "is_staff", "is_superuser", "is_active", "is_verified", "date_joined")
    search_fields = ("email", "display_name", "username", "bio")
    ordering = ("-date_joined",)
    filter_horizontal = ("groups", "user_permissions", "preferred_genres")
    readonly_fields = ("last_login", "last_login_ip", "date_joined", "initials_display", "followed_artists_display")
    list_per_page = 30
    save_on_top = True

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("display_name", "username", "avatar", "bio", "country", "initials_display")}),
        ("Access", {"fields": ("role", "is_active", "is_verified", "is_staff", "is_superuser")}),
        ("Preferences", {"fields": ("preferred_genres", "followed_artists_display")}),
        (
            "Permissions",
            {"fields": ("groups", "user_permissions"), "classes": ("collapse",)},
        ),
        (
            "Activity",
            {
                "fields": ("last_login", "last_login_ip", "date_joined"),
                "classes": ("collapse",),
            },
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "display_name", "role", "password1", "password2"),
            },
        ),
    )
    actions = ("verify_users", "promote_to_staff", "demote_from_staff", "deactivate")

    @admin.display(description="Role", ordering="role")
    def role_badge(self, obj):
        colours = {
            User.Role.LISTENER: "#8A8FA3",
            User.Role.ARTIST: "#00E5FF",
            User.Role.CURATOR: "#B47CFF",
            User.Role.MANAGER: "#00E5A0",
        }
        colour = colours.get(obj.role, "#8A8FA3")
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;'
            'color:{};background:{}1A;border:1px solid {}55">{}</span>',
            colour,
            colour,
            colour,
            obj.get_role_display(),
        )

    @admin.display(description="Initials")
    def initials_display(self, obj):
        return obj.initials

    @admin.display(description="Followed artists")
    def followed_artists_display(self, obj):
        names = list(obj.followed_artists.values_list("name", flat=True)[:25])
        if not names:
            return "—"
        suffix = f" (+{obj.followed_artists.count() - len(names)} more)" if obj.followed_artists.count() > len(names) else ""
        return ", ".join(names) + suffix

    @admin.display(description="Joined", ordering="date_joined")
    def joined(self, obj):
        return obj.date_joined.strftime("%Y-%m-%d")

    @admin.action(description="Mark selected users as verified")
    def verify_users(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"{updated} user(s) verified.", extra_tags="success")

    @admin.action(description="Promote to staff (Manager role)")
    def promote_to_staff(self, request, queryset):
        updated = queryset.update(is_staff=True, role=User.Role.MANAGER)
        self.message_user(request, f"{updated} user(s) promoted.", extra_tags="success")

    @admin.action(description="Demote to listener (remove staff)")
    def demote_from_staff(self, request, queryset):
        queryset = queryset.exclude(is_superuser=True)
        updated = queryset.update(is_staff=False, role=User.Role.LISTENER)
        self.message_user(request, f"{updated} user(s) demoted.")

    @admin.action(description="Deactivate selected users")
    def deactivate(self, request, queryset):
        updated = queryset.exclude(is_superuser=True).update(is_active=False)
        self.message_user(request, f"{updated} user(s) deactivated.", extra_tags="warning")
