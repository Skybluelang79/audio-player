"""Account endpoints: registration, JWT issue/refresh, profile management."""

from django.contrib.auth import get_user_model
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from music.permissions import IsAdminStaff

from .emails import send_email_verification, send_password_reset_email
from .models import UserFollow
from .serializers import (
    ChangePasswordSerializer,
    EmailVerificationConfirmSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserProfileSerializer,
    UserSerializer,
)
from .tokens import build_token_pair, revoke_all_tokens_for_user, revoke_refresh_token

User = get_user_model()


def issue_tokens(user):
    refresh, access = build_token_pair(user)
    return {"access": access, "refresh": refresh, "user": UserSerializer(user).data}


def _user_from_access_token(access_token: str):
    """Resolve the user behind a freshly issued access token, for login auditing."""
    if not access_token:
        return None
    try:
        token = AccessToken(access_token)
        return User.objects.filter(pk=token.get("user_id")).first()
    except TokenError:
        return None


class RegisterView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    @extend_schema(
        tags=["auth"],
        request=RegisterSerializer,
        responses={201: OpenApiResponse(description="Account created; returns access/refresh tokens.")},
        summary="Create an account",
    )
    def post(self, request):
        serializer = RegisterSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        user.record_login(request)
        return Response(issue_tokens(user), status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    serializer_class = TokenObtainPairSerializer

    @extend_schema(
        tags=["auth"],
        summary="Exchange credentials for a JWT pair",
        responses={200: OpenApiResponse(description="access / refresh tokens")},
    )
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            user = _user_from_access_token(response.data.get("access"))
            if user is not None:
                user.record_login(request)
        return response


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]

    @extend_schema(tags=["auth"], summary="Rotate an access token")
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class LogoutView(APIView):
    """Blacklists a refresh token. Safe to call twice."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        summary="Blacklist a refresh token",
        request={"application/json": {"type": "object", "properties": {"refresh": {"type": "string"}}}},
        responses={205: OpenApiResponse(description="Token revoked.")},
    )
    def post(self, request):
        refresh_token = request.data.get("refresh") or request.data.get("refresh_token")
        if not refresh_token:
            return Response(
                {"detail": "A refresh token is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not revoke_refresh_token(refresh_token):
            return Response(
                {"detail": "Token is invalid or already revoked."}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({"detail": "Logged out."}, status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """Read or update the authenticated user's own profile."""

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["auth"], responses={200: UserSerializer}, summary="Current profile")
    def get(self, request):
        return Response(UserSerializer(request.user, context={"request": request}).data)

    @extend_schema(tags=["auth"], request=UserProfileSerializer, responses={200: UserSerializer})
    def patch(self, request):
        serializer = UserProfileSerializer(
            request.user, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user, context={"request": request}).data)


class PasswordChangeView(APIView):
    """Changing a password revokes every outstanding refresh token."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["auth"],
        request=ChangePasswordSerializer,
        responses={204: None},
        summary="Change password",
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "Password updated. All sessions were signed out."},
            status=status.HTTP_200_OK,
        )


class PasswordResetRequestView(APIView):
    """Email a reset link. Always answers generically to avoid enumeration."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        request=PasswordResetRequestSerializer,
        responses={200: OpenApiResponse(description="Reset email sent if the account exists.")},
        summary="Request a password reset",
    )
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            send_password_reset_email(user, request)
        return Response(
            {"detail": "If that email is registered, a reset link has been sent."}
        )


class PasswordResetConfirmView(APIView):
    """Complete a reset with the uid/token from the email."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        request=PasswordResetConfirmSerializer,
        responses={200: None},
        summary="Complete a password reset",
    )
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        # A reset is a security event: sign every other session out.
        revoke_all_tokens_for_user(user)
        return Response({"detail": "Password updated. Please sign in."})


class EmailVerificationRequestView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["auth"],
        request=None,
        responses={200: None},
        summary="Send an email-verification message",
    )
    def post(self, request):
        if request.user.is_verified:
            return Response({"detail": "Email is already verified."})
        send_email_verification(request.user, request)
        return Response({"detail": "Verification email sent."})


class EmailVerificationConfirmView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        request=EmailVerificationConfirmSerializer,
        responses={200: None},
        summary="Confirm an email address",
    )
    def post(self, request):
        serializer = EmailVerificationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        if not user.is_verified:
            user.is_verified = True
            user.save(update_fields=["is_verified"])
        return Response({"detail": "Email verified."})


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """Public member directory with a self-service ``me`` endpoint."""

    queryset = User.objects.filter(is_active=True)
    serializer_class = UserSerializer
    permission_classes = [AllowAny]
    search_fields = ("display_name", "email", "username", "bio")
    ordering_fields = ("date_joined", "last_login", "display_name")
    # Profile rows are read-only through this viewset; only the follow action
    # mutates, and it uses POST/DELETE.
    http_method_names = ["get", "head", "options", "post", "delete"]

    def get_permissions(self):
        if self.action in ("follow",):
            return [IsAuthenticated()]
        return super().get_permissions()

    # Block the DefaultRouter's write mapping (PUT/PATCH/POST/DELETE on the
    # collection and detail routes) without killing the POST/DELETE follow
    # action, which rides the detail route.
    def create(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)

    @extend_schema(
        tags=["auth"],
        request=None,
        responses={200: None},
        summary="Follow / unfollow a listener",
    )
    @action(detail=True, methods=["post", "delete"])
    def follow(self, request, pk=None):
        target = self.get_object()
        if target.pk == request.user.pk:
            return Response(
                {"detail": "You cannot follow yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if request.method == "POST":
            UserFollow.objects.get_or_create(follower=request.user, following=target)
        else:
            UserFollow.objects.filter(follower=request.user, following=target).delete()
        return Response(
            {
                "is_following": request.method == "POST",
                "follower_count": target.follower_relations.count(),
                "following_count": request.user.following_relations.count(),
            }
        )

    @extend_schema(tags=["auth"], responses={200: UserSerializer(many=True)}, summary="Followers")
    @action(detail=True, methods=["get"])
    def followers(self, request, pk=None):
        user = self.get_object()
        # follower_relations holds rows where the profile user is `following`;
        # the same rows reference their followers through the `follower` FK.
        queryset = (
            User.objects.filter(following_relations__following=user)
            .order_by("-following_relations__created_at")
            .distinct()
        )
        return self._paginated_users(queryset)

    @extend_schema(tags=["auth"], responses={200: UserSerializer(many=True)}, summary="Following")
    @action(detail=True, methods=["get"])
    def following(self, request, pk=None):
        user = self.get_object()
        # following_relations holds rows where the profile user is `follower`;
        # the same rows reference who they follow through the `following` FK.
        queryset = (
            User.objects.filter(follower_relations__follower=user)
            .order_by("-follower_relations__created_at")
            .distinct()
        )
        return self._paginated_users(queryset)

    def _paginated_users(self, queryset):
        page = self.paginate_queryset(queryset)
        serializer = UserSerializer(
            page if page is not None else queryset,
            many=True,
            context={"request": self.request},
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(tags=["auth"], responses={200: UserSerializer}, summary="Current user")
    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated], url_path="me")
    def me(self, request):
        return Response(UserSerializer(request.user, context={"request": request}).data)

    @extend_schema(
        tags=["auth"],
        request=UserProfileSerializer,
        responses={200: UserProfileSerializer},
        summary="Update own profile",
    )
    @action(detail=False, methods=["patch", "put"], permission_classes=[IsAuthenticated], url_path="me")
    def update_me(self, request):
        serializer = UserProfileSerializer(
            request.user, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserProfileSerializer(user, context={"request": request}).data)


class StaffUserViewSet(viewsets.ReadOnlyModelViewSet):
    """Staff-only directory with role and verification controls."""

    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = UserProfileSerializer
    permission_classes = [IsAdminStaff]
    filterset_fields = ["role", "is_staff", "is_superuser", "is_verified", "is_active"]
    search_fields = ("email", "display_name", "username")
    ordering_fields = ("date_joined", "last_login")

    @extend_schema(tags=["accounts"], request=None, responses={200: UserProfileSerializer})
    @action(detail=True, methods=["post"], url_path="verify")
    def set_verified(self, request, pk=None):
        user = self.get_object()
        user.is_verified = bool(request.data.get("is_verified", not user.is_verified))
        user.save(update_fields=["is_verified"])
        return Response(UserProfileSerializer(user, context={"request": request}).data)

    @extend_schema(tags=["accounts"], request=None, responses={200: UserProfileSerializer})
    @action(detail=True, methods=["post"])
    def set_role(self, request, pk=None):
        user = self.get_object()
        role = request.data.get("role")
        valid = {value for value, _label in User.Role.choices}
        if role not in valid:
            return Response(
                {"role": [f"Unknown role. Expected one of {sorted(valid)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user == request.user and not user.is_superuser:
            return Response(
                {"detail": "You cannot change your own role."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.is_superuser and not request.user.is_superuser:
            return Response(
                {"detail": "Only a superuser can change a superuser account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user.role = role
        user.is_staff = role == User.Role.MANAGER
        user.save(update_fields=["role", "is_staff"])
        return Response(UserProfileSerializer(user, context={"request": request}).data)
