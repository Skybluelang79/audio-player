"""API v1 routes."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts import views as account_views
from analytics import recommend as recommend_views
from analytics import search as search_views
from analytics import views as analytics_views
from music import views as music_views
from notifications import views as notification_views
from playlists import views as playlist_views

app_name = "api"

router = DefaultRouter()

# --- Accounts -------------------------------------------------------------
router.register("users", account_views.UserViewSet, basename="user")
router.register("staff/users", account_views.StaffUserViewSet, basename="staff-user")

# --- Catalogue ------------------------------------------------------------
router.register("tracks", music_views.TrackViewSet, basename="track")
router.register("artists", music_views.ArtistViewSet, basename="artist")
router.register("albums", music_views.AlbumViewSet, basename="album")
router.register("genres", music_views.GenreViewSet, basename="genre")
router.register("comments", music_views.TrackCommentViewSet, basename="comment")

# --- Playlists ------------------------------------------------------------
router.register("playlists", playlist_views.PlaylistViewSet, basename="playlist")
router.register("favorites", playlist_views.FavoriteViewSet, basename="favorite")
router.register("history", playlist_views.PlayHistoryViewSet, basename="history")

# --- Notifications --------------------------------------------------------
router.register("notifications", notification_views.NotificationViewSet, basename="notification")

# --- Payouts --------------------------------------------------------------
router.register("payouts", analytics_views.PayoutViewSet, basename="payout")

auth_patterns = [
    path("register/", account_views.RegisterView.as_view(), name="register"),
    path("login/", account_views.LoginView.as_view(), name="login"),
    path("refresh/", account_views.RefreshView.as_view(), name="refresh"),
    path("logout/", account_views.LogoutView.as_view(), name="logout"),
    path("me/", account_views.MeView.as_view(), name="me"),
    path(
        "password/change/",
        account_views.PasswordChangeView.as_view(),
        name="password-change",
    ),
    path(
        "password/reset/",
        account_views.PasswordResetRequestView.as_view(),
        name="password-reset",
    ),
    path(
        "password/reset/confirm/",
        account_views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "email/verify/",
        account_views.EmailVerificationRequestView.as_view(),
        name="email-verify",
    ),
    path(
        "email/verify/confirm/",
        account_views.EmailVerificationConfirmView.as_view(),
        name="email-verify-confirm",
    ),
]

catalog_patterns = [
    path("popular/", music_views.PopularTrackView.as_view(), name="tracks-popular"),
    path("trending/", music_views.TrendingTrackView.as_view(), name="tracks-trending"),
]

recommendation_patterns = [
    path("", recommend_views.RecommendedTracksView.as_view(), name="recommendations"),
]

radio_patterns = [
    path("", analytics_views.RadioStationView.as_view(), name="radio"),
]

queue_patterns = [
    path("", playlist_views.PlaybackQueueView.as_view(), name="queue"),
]

analytics_patterns = [
    path("summary/", analytics_views.PlatformStatsView.as_view(), name="analytics-summary"),
    path("top-tracks/", analytics_views.TopTracksView.as_view(), name="analytics-top-tracks"),
    path(
        "trending/",
        analytics_views.TrendingTracksAnalyticsView.as_view(),
        name="analytics-trending",
    ),
    path("artists/", analytics_views.ArtistLeaderboardView.as_view(), name="analytics-artists"),
    path("rebuild/", analytics_views.RebuildStatsView.as_view(), name="analytics-rebuild"),
    path(
        "daily/",
        analytics_views.DailyStatsViewSet.as_view({"get": "list"}),
        name="analytics-daily",
    ),
    path("events/", analytics_views.PlayEventAdminViewSet.as_view({"get": "list"}), name="analytics-events"),
    path("tracks/<int:pk>/", analytics_views.TrackAnalyticsView.as_view(), name="analytics-track"),
]

payout_patterns = [
    path("generate/", analytics_views.PayoutGenerateView.as_view(), name="payout-generate"),
    path("summary/", analytics_views.PayoutSummaryView.as_view(), name="payout-summary"),
]

search_patterns = [
    path("", search_views.GlobalSearchView.as_view(), name="search"),
    path("suggest/", search_views.SearchSuggestionsView.as_view(), name="search-suggest"),
    path("tracks/", music_views.TrackSearchView.as_view(), name="search-tracks"),
]

# Listener rollups. Mounted explicitly because these are plain APIViews rather
# than router-registered viewsets.
history_patterns = [
    path("stats/summary/", playlist_views.ListenStatsView.as_view(), name="history-summary"),
]

urlpatterns = [
    path("auth/", include(auth_patterns)),
    path("catalog/", include(catalog_patterns)),
    path("analytics/", include(analytics_patterns)),
    path("search/", include(search_patterns)),
    path("history/", include(history_patterns)),
    # Mounted before the catch-all router so "recommendations/" is not swallowed.
    path("recommendations/", include(recommendation_patterns)),
    path("radio/", include(radio_patterns)),
    path("queue/", include(queue_patterns)),
    # Explicit staff/artist payout tools must precede the router's <pk> route.
    path("payouts/", include(payout_patterns)),
    # HLS sub-playlists and segments live below the track detail route.
    path(
        "tracks/<int:pk>/hls/<path:path>",
        music_views.track_hls_file,
        name="track-hls-file",
    ),
    path("", include(router.urls)),
]
