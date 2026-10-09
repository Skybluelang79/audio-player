"""Reusable permission classes."""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdminStaff(BasePermission):
    """Superuser or any active staff account."""

    message = "Staff access is required for this operation."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_admin_staff)


class IsOwnerOrReadOnly(BasePermission):
    """Writes are restricted to the object's owner; reads are public."""

    message = "You can only modify resources you own."

    owner_field = "owner"

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        owner = getattr(obj, self.owner_field, None)
        return owner == request.user


class IsPlaylistOwnerOrCollaborator(BasePermission):
    """Owner or explicit collaborator may mutate a playlist."""

    message = "You are not a collaborator on this playlist."

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return obj.is_visible_to(request.user)
        if obj.owner_id == request.user.id:
            return True
        return obj.collaborators.filter(pk=request.user.pk).exists()


class IsArtistManagerOrReadOnly(BasePermission):
    """Staff, or the linked artist account, may edit an artist profile."""

    message = "Only staff or the artist account can modify this profile."

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if request.user.is_admin_staff:
            return True
        return obj.owner_id == request.user.pk
