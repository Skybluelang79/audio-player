"""Shared pytest configuration.

The project pins ``PASSWORD_HASHERS`` in ``config/settings.py`` to PBKDF2, which
costs ~4.5s per hash on this machine. That is the right default for production
but dominates the test suite: every ``create_user`` in the suite pays it, and
the suite grew from minutes to ~13. Django's own runner normally substitutes a
fast hasher during tests, but an explicit ``PASSWORD_HASHERS`` setting opts out
of that, so it is done here instead.

Only iteration count changes -- the algorithm, salt handling and verification
path stay identical, so tests still exercise real hashing semantics.
"""

import pytest
from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher


class _FastTestHasher(PBKDF2PasswordHasher):
    iterations = 1


@pytest.fixture(scope="session", autouse=True)
def fast_password_hashing(django_test_environment):
    # Depend on django_test_environment so Django is configured before the
    # override is applied (it is also what already ran setup_test_environment).
    original = list(settings.PASSWORD_HASHERS)
    settings.PASSWORD_HASHERS = [
        f"{_FastTestHasher.__module__}.{_FastTestHasher.__qualname__}"
    ]
    yield
    settings.PASSWORD_HASHERS = original
