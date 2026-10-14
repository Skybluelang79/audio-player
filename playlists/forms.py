"""Form for the listener-facing playlist editor.

Only the fields a curator should touch are exposed; ``owner``, ``is_favorite``
and the computed counters are set by the view/model, never by the form.
"""

from django import forms

from music.storage import validate_image_extension, validate_upload_size

from .models import Playlist


class PlaylistForm(forms.ModelForm):
    class Meta:
        model = Playlist
        fields = ("name", "description", "visibility", "cover_image")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4, "placeholder": "Optional"}),
        }

    def clean_cover_image(self):
        """Mirror the model validators, which ``ModelForm`` skips on upload.

        The model's ``ImageField`` validators only run through ``full_clean``;
        a ``ModelForm`` does not call it, so the extension and size rules are
        enforced here or not at all.
        """
        cover = self.cleaned_data.get("cover_image")
        if cover:
            validate_image_extension(cover.name)
            validate_upload_size(cover)
        return cover
