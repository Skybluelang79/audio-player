"""Account serializers.

Write operations are deliberately split from read operations: profile updates
never accept privilege-bearing fields (``is_staff``, ``is_superuser``, ``role``).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from music.models import Genre

from .tokens import revoke_all_tokens_for_user

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Public read model."""

    full_name = serializers.CharField(read_only=True)
    initials = serializers.CharField(read_only=True)
    favorite_count = serializers.SerializerMethodField()
    playlist_count = serializers.SerializerMethodField()
    follower_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "display_name", "full_name", "initials", "avatar",
            "bio", "country", "role", "is_verified", "is_staff", "date_joined",
            "last_login", "favorite_count", "playlist_count", "follower_count",
            "following_count", "is_following",
        )
        read_only_fields = fields

    def get_favorite_count(self, obj) -> int:
        annotated = getattr(obj, "favorite_count", None)
        return annotated if annotated is not None else obj.favorites.count()

    def get_playlist_count(self, obj) -> int:
        annotated = getattr(obj, "playlist_count", None)
        return annotated if annotated is not None else obj.playlists.count()

    def get_follower_count(self, obj) -> int:
        annotated = getattr(obj, "follower_total", None)
        return annotated if annotated is not None else obj.follower_relations.count()

    def get_following_count(self, obj) -> int:
        annotated = getattr(obj, "following_total", None)
        return annotated if annotated is not None else obj.following_relations.count()

    def get_is_following(self, obj) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        if obj.pk == request.user.pk:
            return False
        return obj.follower_relations.filter(follower=request.user).exists()


class UserProfileSerializer(serializers.ModelSerializer):
    """Write model for ``PATCH /api/auth/me/``.

    Deliberately has no password field: a profile update must never be able to
    change the password. Passwords are only changed through
    ``ChangePasswordSerializer``, which requires the current password.
    """

    preferred_genres = serializers.PrimaryKeyRelatedField(
        many=True, required=False, queryset=Genre.objects.all()
    )

    class Meta:
        model = User
        fields = (
            "id", "email", "display_name", "avatar", "bio", "country",
            "preferred_genres", "last_login", "last_login_ip", "is_verified",
            "role", "is_staff",
        )
        read_only_fields = ("id", "last_login", "last_login_ip", "is_verified", "role", "is_staff")

    def validate_email(self, value):
        if not value:
            raise serializers.ValidationError("An email address is required.")
        return value.strip().lower()

    def update(self, instance, validated_data):
        genres = validated_data.pop("preferred_genres", None)
        user = super().update(instance, validated_data)
        if genres is not None:
            user.preferred_genres.set(genres)
        return user


class RegisterSerializer(serializers.ModelSerializer):
    """Self-service signup.

    ``role`` is intentionally not writable here: signup always creates a
    listener, and role/staff changes are staff-only operations.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})
    display_name = serializers.CharField(required=False, allow_blank=True, max_length=120)

    class Meta:
        model = User
        fields = ("id", "email", "password", "password_confirm", "display_name", "role")
        read_only_fields = ("id", "role")

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs.get("password"), User(email=attrs.get("email", "")))
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm", None)
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    new_password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        if value == self.initial_data.get("current_password"):
            raise serializers.ValidationError("The new password must differ from the current one.")
        validate_password(value, self.context["request"].user)
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        revoke_all_tokens_for_user(user)
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class _UidTokenSerializer(serializers.Serializer):
    """Shared uid/token resolution for reset and verification links.

    ``resolve_user(attrs)`` is safe to call from ``validate()``: it reads the
    already-cleaned ``attrs`` (DRF has not built ``validated_data`` yet while
    ``validate`` is running).
    """

    uid = serializers.CharField()
    token = serializers.CharField()

    def resolve_user(self, attrs):
        try:
            pk = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=pk, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({"uid": "Invalid link."})

        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError(
                {"token": "This link is invalid or has expired."}
            )
        return user


class PasswordResetConfirmSerializer(_UidTokenSerializer):
    new_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    new_password_confirm = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password_confirm": "Passwords do not match."}
            )
        user = self.resolve_user(attrs)
        validate_password(attrs["new_password"], user)
        attrs["user"] = user
        return attrs


class EmailVerificationConfirmSerializer(_UidTokenSerializer):
    def validate(self, attrs):
        attrs["user"] = self.resolve_user(attrs)
        return attrs
