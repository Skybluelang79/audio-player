"""Activity notifications delivered to listeners.

Rows are created by ``notifications.signals`` when a social action happens
(a follow, a comment reply, a playlist share). The model intentionally stores a
denormalised ``text`` and ``url`` so rendering a notification feed never has to
re-resolve the target, which may since have been deleted or made private.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    class Kind(models.TextChoices):
        FOLLOW = "follow", "Someone followed you"
        ARTIST_FOLLOW = "artist_follow", "An artist you own gained a follower"
        COMMENT = "comment", "New comment on a track you own"
        COMMENT_REPLY = "comment_reply", "Reply to your comment"
        PLAYLIST_FOLLOW = "playlist_follow", "Someone followed your playlist"
        PLAYLIST_COLLAB = "playlist_collab", "You were added to a playlist"
        SYSTEM = "system", "Announcement"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acted_notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    text = models.CharField(max_length=255)
    url = models.CharField(max_length=500, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient", "read_at"]),
        ]

    def __str__(self):
        return f"{self.kind}: {self.text}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self, *, save: bool = True):
        if self.read_at is None:
            self.read_at = timezone.now()
            if save:
                self.save(update_fields=["read_at"])
        return self.read_at


def notify(*, recipient, kind, text, actor=None, url="") -> Notification | None:
    """Create a notification, dropping no-ops (self-actions, anonymous targets)."""
    if recipient is None:
        return None
    if actor is not None and getattr(actor, "pk", None) == recipient.pk:
        return None
    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        kind=kind,
        text=text[:255],
        url=url[:500],
    )
