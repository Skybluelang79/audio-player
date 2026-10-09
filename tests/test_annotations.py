"""Guard-rail tests for two mistakes that silently break the API.

1. Annotating a name that a model already exposes as a ``@property``. The
   property is a data descriptor, so the annotation is ignored on read, and
   Django raises ``AttributeError: property ... has no setter`` when it
   instantiates the model from an annotated row.
2. Relying on ``Meta.ordering`` for a queryset that carries a multi-valued
   aggregate annotation. Django drops the default ordering there, so listing
   endpoints become non-deterministic and pagination overlaps/skips rows.
"""

import ast
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase

PROJECT_ROOT = Path(settings.BASE_DIR)


def _resolve_model(model_name: str):
    """Find a model by class name across all apps, or ``None``.

    ``apps.get_model`` expects an ``app_label.ModelName`` string, so resolve the
    class name by hand.
    """
    for model in apps.get_models(include_auto_created=True):
        if model.__name__ == model_name:
            return model
    return None


def _property_names(model) -> set[str]:
    """Names exposed as ``@property`` on a model's class hierarchy.

    Walk ``model.__mro__`` — ``type(model).__mro__`` is the *metaclass* chain and
    would silently see none of the model's own properties.
    """
    return {
        name
        for klass in reversed(model.__mro__)
        for name, member in vars(klass).items()
        if isinstance(member, property)
    }


def _annotated_names(call_node) -> set[str]:
    """Keyword names passed to ``.annotate(...)``/``.alias(...)``."""
    names = set()
    for keyword in call_node.keywords:
        if keyword.arg is not None:
            names.add(keyword.arg)
    return names


class AnnotationCollisionTests(SimpleTestCase):
    def test_annotated_querysets_never_shadow_a_model_property(self):
        """No ``.annotate(x=...)`` may reuse a property name of the queryset's model."""
        offenders = []

        for path in sorted(PROJECT_ROOT.rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            if not _is_project_file(relative):
                continue

            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"annotate", "alias"}:
                    continue
                for name in _annotated_names(node):
                    for model in apps.get_models():
                        if name in _property_names(model):
                            offenders.append(
                                f"{relative}:{node.lineno} annotates {name!r}, "
                                f"which is a property of {model.__name__}"
                            )

        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_counter_serializer_helper_prefers_agg_aliases(self):
        """``music.serializers.counter`` must prefer the batched alias."""
        from music.serializers import counter

        class Fake:
            agg_track_count = 7
            track_count = 1

        self.assertEqual(counter(Fake(), "track_count"), 7)

        class Fallback:
            track_count = 4

        self.assertEqual(counter(Fallback(), "track_count"), 4)
        self.assertEqual(counter(object(), "track_count"), 0)


class OrderingTests(SimpleTestCase):
    def test_playlist_listing_is_deterministically_ordered(self):
        """Django drops ``Meta.ordering`` for aggregate-annotated querysets, so
        the paginated playlist list must declare its own default ordering."""
        from playlists.filters import with_playlist_counters
        from playlists.models import Playlist
        from playlists.views import PlaylistViewSet

        queryset = with_playlist_counters(Playlist.objects.all())

        self.assertFalse(queryset.ordered, "aggregate annotation clears Meta.ordering")
        self.assertTrue(PlaylistViewSet.ordering, "viewset must pin its own ordering")

    def test_aggregated_list_querysets_are_ordered(self):
        """Every aggregated queryset exposed by a list view is explicitly ordered."""
        from playlists.filters import with_playlist_counters
        from playlists.models import Playlist
        from playlists.views import PlaylistViewSet

        queryset = with_playlist_counters(
            Playlist.objects.filter(visibility=Playlist.Visibility.PUBLIC)
        ).order_by(*PlaylistViewSet.ordering)
        self.assertTrue(queryset.ordered)
        self.assertEqual(PlaylistViewSet.ordering[0], "-created_at")

    def test_genre_ordering_alias_maps_to_the_real_annotation(self):
        """?ordering=track_count must resolve to the agg_ alias, not FieldError."""
        from music.views import GenreViewSet

        self.assertEqual(GenreViewSet.ordering_aliases["track_count"], "agg_track_count")

    def test_ordering_introspection_actually_inspects_something(self):
        """Guard against the AST scan silently matching zero annotate() calls."""
        scanned = sum(len(aliases) for aliases in _annotations_by_model().values())
        self.assertGreater(scanned, 0)


MODEL_NAMES = frozenset(
    {"Track", "Artist", "Album", "Genre", "Playlist", "Favorite", "PlayHistory", "User"}
)

#: Top-level directories under BASE_DIR that are first-party app code.
PROJECT_APPS = frozenset({"accounts", "music", "playlists", "analytics", "dashboard"})


def _is_project_file(relative: Path) -> bool:
    """True for Python files belonging to a first-party Django app."""
    if len(relative.parts) < 2:
        return False
    if relative.parts[0] not in PROJECT_APPS:
        return False
    return "migrations" not in relative.parts


def _annotates_model_attributes(node) -> bool:
    """False for ``.values(...).annotate(...)``, whose aliases become dict keys.

    ``.values()`` turns the row into a dict, so an alias like ``tracks=Count(...)``
    is just a result key and can never clash with a model property. Only
    annotate() calls that still yield model instances are interesting.
    """
    receiver = node.func.value
    while isinstance(receiver, ast.Call):
        func = receiver.func
        if isinstance(func, ast.Attribute) and func.attr in {"values", "values_list"}:
            return False
        receiver = func.value
    return True


def _annotations_by_model() -> dict:
    """Map model -> annotation aliases that land on model instances."""
    mapping: dict = {}
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if not _is_project_file(relative):
            continue

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"annotate", "alias"}:
                continue
            if not _annotates_model_attributes(node):
                continue
            aliases = _annotated_names(node)
            if not aliases:
                continue

            segment = ast.get_source_segment(source, node) or ""
            mentioned = {name for name in MODEL_NAMES if re.search(rf"\b{name}\b", segment)}
            for model_name in mentioned:
                model = _resolve_model(model_name)
                if model is None:
                    continue
                mapping.setdefault(model, set()).update(aliases)
    return mapping
