"""Forms for the dashboard's quick-upload flow."""

from django import forms

from .models import Album, Artist, Genre, Track
from .storage import validate_audio_extension, validate_image_extension, validate_upload_size

AUDIO_ACCEPT = ".mp3,.m4a,.aac,.wav,.flac,.ogg,.oga,.opus,.wma,.aiff"
IMAGE_ACCEPT = ".jpg,.jpeg,.png,.webp,.gif,.bmp,.avif"


class UploadedFileField(forms.FileField):
    """A ``FileField`` that keeps the ``UploadedFile`` object.

    Django's ``forms.FileField.to_python()`` returns ``data.name`` — a plain
    string. In a ``ModelForm`` that value is then handed to
    ``FileField.save_form_data()``, which stores it as an already-committed
    name, so the bytes are never copied into storage and the database row ends
    up pointing at a file that does not exist. Run the stock validation, then
    hand back the original object so ``save()`` performs the real upload.
    """

    def to_python(self, data):
        super().to_python(data)
        return data


class QuickTrackUploadForm(forms.ModelForm):
    """Single-step track upload: audio + cover art + metadata in one form.

    A new artist can be created inline by typing a name into ``artist_name``.
    """

    audio_file = UploadedFileField(
        widget=forms.FileInput(attrs={"accept": AUDIO_ACCEPT}),
        error_messages={"invalid": "Upload a valid audio file."},
    )
    cover_image = UploadedFileField(
        required=False,
        widget=forms.FileInput(attrs={"accept": IMAGE_ACCEPT}),
        error_messages={"invalid": "Upload a valid image file."},
    )
    artist_name = forms.CharField(
        required=False,
        max_length=180,
        label="New artist",
        help_text="Optional — creates the artist when no existing one is selected.",
    )
    genres = forms.ModelMultipleChoiceField(queryset=Genre.objects.all(), required=False)
    consent = forms.BooleanField(
        required=True,
        label="I confirm I hold the rights to distribute this audio.",
    )

    class Meta:
        model = Track
        fields = (
            "title", "artist", "artist_name", "album", "audio_file", "cover_image",
            "duration_seconds", "bitrate_kbps", "sample_rate_hz", "track_number",
            "isrc", "explicit", "release_date", "status", "lyrics",
        )
        widgets = {
            "lyrics": forms.Textarea(attrs={"rows": 4, "placeholder": "Optional"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["album"].queryset = Album.objects.select_related("artist").order_by("title")
        self.fields["album"].empty_label = "— single, no album —"
        # ``status`` has a model-level default of DRAFT, so treat it as optional
        # here and fall back to it, rather than rejecting the upload outright.
        self.fields["status"].required = False
        self.fields["status"].initial = Track.Status.DRAFT
        # ``artist`` is satisfied by either the dropdown or ``artist_name``, so it
        # must not be individually required; ``clean_artist`` enforces "one of".
        self.fields["artist"].required = False
        self.fields["artist"].empty_label = "— select an existing artist —"
        for name in ("cover_image", "isrc", "track_number", "bitrate_kbps",
                     "sample_rate_hz", "explicit", "release_date", "duration_seconds"):
            self.fields[name].required = False

    def clean_audio_file(self):
        audio = self.cleaned_data.get("audio_file")
        if audio:
            validate_audio_extension(audio.name)
            validate_upload_size(audio)
        return audio

    def clean_cover_image(self):
        cover = self.cleaned_data.get("cover_image")
        if cover:
            validate_image_extension(cover.name)
            validate_upload_size(cover)
        return cover

    def clean_artist(self):
        artist = self.cleaned_data.get("artist")
        # Read from self.data, not cleaned_data: ``artist_name`` is declared after
        # ``artist``, so it has not been cleaned yet when this method runs.
        new_name = (self.data.get("artist_name") or "").strip()
        if artist:
            return artist
        if new_name:
            # Names are deliberately not unique, so never use get_or_create here.
            artist = Artist.objects.create(
                name=new_name, bio="Created from a quick upload."
            )
            return artist
        raise forms.ValidationError("Choose an artist or type a new artist name.")

    def clean_status(self):
        return self.cleaned_data.get("status") or Track.Status.DRAFT

    def clean_consent(self):
        if not self.cleaned_data.get("consent"):
            raise forms.ValidationError("You must confirm distribution rights to upload.")
        return True

    def clean(self):
        """Cross-field validation. Note the signature is ``clean(self)`` — Django
        passes no ``cleaned_data`` here, so declaring one raises TypeError."""
        cleaned_data = super().clean()
        album = cleaned_data.get("album")
        artist = cleaned_data.get("artist")
        if album and artist and album.artist_id != artist.pk:
            self.add_error("album", "That album belongs to a different artist.")
        return cleaned_data

    def save(self, commit=True):
        track = super().save(commit=False)
        if commit:
            # M2M relations need a primary key, so persist before assigning.
            track.save()
            genres = self.cleaned_data.get("genres")
            if genres:
                track.genres.set(genres)
        return track


class StudioTrackUploadForm(QuickTrackUploadForm):
    """Quick upload pinned to one artist for the artist studio.

    The owning artist is hidden from the artist (they can only ever release
    under their own handle); the rightsholder consent still applies.
    """

    def __init__(self, *args, artist=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_artist = artist
        self.fields.pop("artist_name", None)
        self.fields["artist"].widget = forms.HiddenInput()
        if artist is not None:
            self.fields["artist"].queryset = Artist.objects.filter(pk=artist.pk)
            self.fields["artist"].initial = artist.pk

    def clean_artist(self):
        if self._pinned_artist is not None:
            return self._pinned_artist
        artist = self.cleaned_data.get("artist")
        if artist is None:
            raise forms.ValidationError("Artist is required.")
        return artist


class ArtistProfileForm(forms.ModelForm):
    """An artist fills in (or updates) the public face of their profile."""

    class Meta:
        model = Artist
        fields = ("bio", "website", "country", "image", "genres")
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if image:
            validate_image_extension(image.name)
            validate_upload_size(image)
        return image


class ArtistClaimForm(forms.Form):
    """Onboarding: create a brand-new profile or claim an unclaimed one."""

    MODE_CHOICES = (
        ("create", "Create a new profile"),
        ("claim", "Claim an existing profile"),
    )

    mode = forms.ChoiceField(choices=MODE_CHOICES, initial="create")
    name = forms.CharField(
        max_length=180, required=False, label="Artist name (new profile)"
    )
    existing_artist = forms.ModelChoiceField(
        queryset=Artist.objects.none(),
        required=False,
        label="Unclaimed profile",
        help_text="Profiles nobody owns yet, sorted A–Z.",
    )
    country = forms.CharField(max_length=64, required=False)
    website = forms.URLField(required=False)
    bio = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False, max_length=3000
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["existing_artist"].queryset = (
            Artist.objects.filter(owner__isnull=True).order_by("name")
        )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        if mode == "create" and not (cleaned.get("name") or "").strip():
            self.add_error("name", "Enter a name for your new profile.")
        if mode == "claim" and not cleaned.get("existing_artist"):
            self.add_error("existing_artist", "Choose a profile to claim.")
        return cleaned
