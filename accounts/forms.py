"""Form for the listener-facing profile editor."""

from django import forms
from django.contrib.auth import get_user_model

from music.storage import validate_image_extension, validate_upload_size

User = get_user_model()


class ProfileForm(forms.ModelForm):
    """Edit the public face of a listener's own profile.

    Deliberately excludes ``email`` and every privilege-bearing field
    (``role``, ``is_staff``, ``is_verified``): those stay API/admin-only so this
    page can never escalate an account or change the login handle.
    """

    class Meta:
        model = User
        fields = ("display_name", "bio", "country", "avatar")
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4, "placeholder": "Optional"}),
        }

    def clean_avatar(self):
        """Enforce the image rules the model leaves to callers.

        ``User.avatar`` has no validators (unlike cover art on the catalogue
        models), so the extension and size checks live here.
        """
        avatar = self.cleaned_data.get("avatar")
        if avatar:
            validate_image_extension(avatar.name)
            validate_upload_size(avatar)
        return avatar
