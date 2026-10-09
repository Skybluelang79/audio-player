"""JWT helpers built on top of SimpleJWT.

The blacklist app is optional so a minimal deployment still works; when it is
not installed, ``revoke_refresh_token`` simply reports failure instead of
raising.
"""

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

try:  # pragma: no cover - depends on INSTALLED_APPS
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    BLACKLIST_AVAILABLE = True
except ImportError:  # pragma: no cover
    BlacklistedToken = None
    OutstandingToken = None
    BLACKLIST_AVAILABLE = False


def build_token_pair(user) -> tuple[str, str]:
    """Return ``(refresh, access)`` for ``user``."""
    refresh = RefreshToken.for_user(user)
    refresh["email"] = user.email
    refresh["role"] = user.role
    refresh["is_staff"] = user.is_staff
    return str(refresh), str(refresh.access_token)


def revoke_refresh_token(token_string: str) -> bool:
    """Blacklist a refresh token. Returns ``False`` if it was already revoked."""
    if not BLACKLIST_AVAILABLE:
        return False
    try:
        token = RefreshToken(token_string)
    except TokenError:
        return False
    if OutstandingToken.objects.filter(
        jti=token["jti"], blacklistedtoken__isnull=False
    ).exists():
        return False
    token.blacklist()
    return True


def revoke_all_tokens_for_user(user) -> int:
    """Invalidate every outstanding refresh token belonging to ``user``."""
    if not BLACKLIST_AVAILABLE:
        return 0
    tokens = list(OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True))
    for token in tokens:
        try:
            RefreshToken(str(token.token)).blacklist()
        except TokenError:  # pragma: no cover - row is already unusable
            continue
    return len(tokens)
