import uuid

from django.contrib.auth.models import AbstractUser, UserManager as BaseUserManager
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class UserManager(BaseUserManager):
    """Manager that treats email as the primary handle.

    ``username`` is optional on this model, so the base manager's positional
    ``username`` argument would break every ``create_user`` call. It is derived
    from the local part of the email address instead.
    """

    use_in_migrations = True

    def _derive_username(self, email, username=None):
        if username:
            return username
        local = (email or "").split("@")[0]
        return local or "user"

    def create_user(self, email=None, password=None, username=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        extra_fields.setdefault("username", self._derive_username(email, username))
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email=None, password=None, username=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password=password, username=username, **extra_fields)

    def get_by_natural_key(self, username):
        return self.get(**{self.model.USERNAME_FIELD: username})


class User(AbstractUser):
    """Account model.

    Email is the login handle; ``username`` is kept for compatibility with
    Django admin and third-party tooling but is no longer unique or required.
    """

    class Role(models.TextChoices):
        LISTENER = "listener", "Listener"
        ARTIST = "artist", "Artist"
        CURATOR = "curator", "Curator"
        MANAGER = "manager", "Manager"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    username = models.CharField(max_length=150, blank=True, null=True)
    display_name = models.CharField(max_length=120, blank=True)
    bio = models.TextField(blank=True, max_length=1000)
    avatar = models.ImageField(upload_to="avatars/%Y/%m/", blank=True, null=True)
    country = models.CharField(max_length=64, blank=True)
    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.LISTENER, db_index=True
    )
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    preferred_genres = models.ManyToManyField("music.Genre", related_name="preferred_by", blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = "accounts_user"
        ordering = ("-date_joined",)
        indexes = [models.Index(fields=["role", "-date_joined"])]
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return self.display_name or self.email

    def save(self, *args, **kwargs):
        if not self.username:
            self.username = self.email.split("@")[0]
        if not self.display_name:
            self.display_name = self.username or self.email.split("@")[0]
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        name = self.get_full_name().strip()
        return name or self.display_name or self.email

    @property
    def is_admin_staff(self) -> bool:
        """Dashboard access: superuser, or any active staff account."""
        return bool((self.is_superuser or self.is_staff) and self.is_active)

    @property
    def follower_count(self) -> int:
        return self.follower_relations.count()

    @property
    def following_count(self) -> int:
        return self.following_relations.count()

    def is_following_user(self, other) -> bool:
        if not other or not getattr(other, "pk", None):
            return False
        return self.following_relations.filter(following=other).exists()

    @property
    def initials(self) -> str:
        parts = [p for p in self.full_name.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def record_login(self, request=None):
        self.last_login = timezone.now()
        if request is not None:
            self.last_login_ip = self.client_ip(request)
        self.save(update_fields=["last_login", "last_login_ip"])
        return self

    @staticmethod
    def client_ip(request) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        raw = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
        try:
            import ipaddress

            return str(ipaddress.ip_address(raw))
        except ValueError:
            return ""


class UserFollow(models.Model):
    """A one-directional follow between two listeners.

    Distinct from `Artist.followers`: this is the social graph the activity feed
    reads, not a subscription to a catalogue entity.
    """

    follower = models.ForeignKey(
        "accounts.User", related_name="following_relations", on_delete=models.CASCADE
    )
    following = models.ForeignKey(
        "accounts.User", related_name="follower_relations", on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "accounts_user_follow"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["follower", "following"], name="uniq_user_follow"
            ),
            # A self-follow would make the feed and counts nonsensical and is
            # never a meaningful action.
            models.CheckConstraint(
                check=~Q(follower=F("following")), name="no_self_follow"
            ),
        ]
        indexes = [
            models.Index(fields=["follower", "-created_at"]),
            models.Index(fields=["following", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.follower_id} follows {self.following_id}"
