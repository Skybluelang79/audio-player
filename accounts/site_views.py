"""Listener-facing account pages: public profiles and the activity feed.

These mirror the REST API (`/api/users/...`) but render HTML for the web app.
The privacy rules match ``music.site_views.visible_playlists``: a profile only
ever shows playlists the viewer is allowed to see.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import DetailView, TemplateView, UpdateView

from music.models import Track
from music.site_views import SiteViewMixin, visible_playlists

from .forms import ProfileForm

User = get_user_model()


class ProfileView(SiteViewMixin, DetailView):
    """A listener's public profile plus their social graph."""

    model = User
    template_name = "accounts/profile.html"
    context_object_name = "profile_user"

    def get_queryset(self):
        return User.objects.filter(is_active=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile_user = self.object
        viewer = self.request.user
        is_self = viewer.is_authenticated and viewer.pk == profile_user.pk

        context.update(
            {
                "is_self": is_self,
                "is_following": (
                    viewer.is_authenticated
                    and viewer.pk != profile_user.pk
                    and profile_user.follower_relations.filter(follower=viewer).exists()
                ),
                "followers_count": profile_user.follower_relations.count(),
                "following_count": profile_user.following_relations.count(),
                "public_playlists": visible_playlists(
                    profile_user, public_only=True
                ).filter(owner=profile_user)[:12],
                "following_artists": profile_user.followed_artists.all()[:12],
            }
        )
        # Favourites are private: only the owner sees their own on this page.
        if is_self:
            context["favorites"] = (
                Track.objects.published()
                .with_listeners()
                .filter(favorited_by__user=profile_user)
                .distinct()[:20]
            )
        return context


class ProfileEditView(LoginRequiredMixin, SiteViewMixin, UpdateView):
    """A listener edits their own profile.

    There is no pk in the URL: the view only ever operates on ``request.user``,
    so it is impossible to edit anyone else's account through this page.
    """

    form_class = ProfileForm
    template_name = "accounts/profile_form.html"

    def get_object(self, queryset=None):
        return self.request.user

    def get_success_url(self):
        return reverse("accounts:profile", kwargs={"pk": self.request.user.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Profile updated.")
        return response


class FeedView(LoginRequiredMixin, SiteViewMixin, TemplateView):
    """What's new from the artists and listeners ``request.user`` follows."""

    template_name = "accounts/feed.html"
    nav_active = "feed"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        followed_ids = user.following_relations.values_list("following_id", flat=True)
        artist_ids = user.followed_artists.values_list("id", flat=True)

        context.update(
            {
                "new_from_artists": (
                    Track.objects.published()
                    .with_listeners()
                    .filter(artist_id__in=artist_ids)
                    .order_by("-release_date", "-created_at")[:12]
                ),
                "playlists": visible_playlists(user, public_only=True)
                .filter(owner_id__in=followed_ids)
                .order_by("-created_at")[:12],
            }
        )
        return context
