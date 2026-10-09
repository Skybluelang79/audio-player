"""Signal receivers that turn social actions into notifications.

Kept deliberately narrow: only the events a listener would actually want to
know about, and never a self-notification (``notify`` guards that).
"""

from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from django.urls import reverse

from accounts.models import UserFollow
from music.models import Artist, TrackComment
from playlists.models import PlaylistFollow

from .models import Notification, notify

User = get_user_model()


def _display(user) -> str:
    return user.display_name or user.email


@receiver(post_save, sender=UserFollow, dispatch_uid="notify_on_user_follow")
def on_user_follow(sender, instance, created, **kwargs):
    if not created:
        return
    notify(
        recipient=instance.following,
        actor=instance.follower,
        kind=Notification.Kind.FOLLOW,
        text=f"{_display(instance.follower)} started following you",
        url=reverse("accounts:profile", kwargs={"pk": instance.follower.pk}),
    )


@receiver(post_save, sender=PlaylistFollow, dispatch_uid="notify_on_playlist_follow")
def on_playlist_follow(sender, instance, created, **kwargs):
    if not created:
        return
    playlist = instance.playlist
    if playlist.owner_id in (None, instance.user_id):
        return
    notify(
        recipient=playlist.owner,
        actor=instance.user,
        kind=Notification.Kind.PLAYLIST_FOLLOW,
        text=f"{_display(instance.user)} followed your playlist “{playlist.name}”",
        url=reverse("playlists:detail", kwargs={"slug": playlist.slug}),
    )


@receiver(post_save, sender=TrackComment, dispatch_uid="notify_on_track_comment")
def on_track_comment(sender, instance, created, **kwargs):
    if not created or instance.status != TrackComment.Status.VISIBLE:
        return

    track_url = reverse("music:track-detail", kwargs={"slug": instance.track.slug})

    # A reply pings the parent's author.
    if instance.parent_id:
        parent_author = instance.parent.user
        if parent_author.pk != instance.user_id:
            notify(
                recipient=parent_author,
                actor=instance.user,
                kind=Notification.Kind.COMMENT_REPLY,
                text=(
                    f"{_display(instance.user)} replied to your comment on "
                    f"“{instance.track.title}”"
                ),
                url=f"{track_url}#comment-{instance.pk}",
            )

    # The artist owner hears about every comment on their catalogue, including
    # replies (they may not be the parent author).
    owner = getattr(instance.track.artist, "owner", None)
    if owner is not None and owner.pk != instance.user_id:
        notify(
            recipient=owner,
            actor=instance.user,
            kind=Notification.Kind.COMMENT,
            text=(
                f"{_display(instance.user)} commented on "
                f"“{instance.track.title}”"
            ),
            url=f"{track_url}#comment-{instance.pk}",
        )


@receiver(
    m2m_changed,
    sender=Artist.followers.through,
    dispatch_uid="notify_on_artist_follow",
)
def on_artist_follow(sender, instance, action, pk_set, **kwargs):
    # `reverse` for m2m_changed is a boolean flag (from the "other side" of the
    # relation), which would shadow the imported URL helper, so read it from
    # kwargs instead of the signature.
    reverse_relation = kwargs.get("reverse", False)
    if reverse_relation or action != "post_add":
        return
    owner = instance.owner
    if owner is None:
        return
    for user in User.objects.filter(pk__in=pk_set or []):
        notify(
            recipient=owner,
            actor=user,
            kind=Notification.Kind.ARTIST_FOLLOW,
            text=f"{_display(user)} followed {instance.name}",
            url=reverse("music:artist-detail", kwargs={"slug": instance.slug}),
        )
