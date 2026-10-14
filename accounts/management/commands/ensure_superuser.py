import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Create a bootstrap superuser from SUPERUSER_EMAIL/SUPERUSER_PASSWORD "
        "when no account with that email exists yet. Safe to run on every boot: "
        "it is a no-op when either variable is unset or the account already "
        "exists, which also lets an ephemeral (SQLite) deployment keep a staff "
        "login across restarts."
    )

    def handle(self, *args, **options):
        email = os.environ.get("SUPERUSER_EMAIL", "").strip()
        password = os.environ.get("SUPERUSER_PASSWORD", "")
        if not email or not password:
            self.stdout.write("SUPERUSER_EMAIL/SUPERUSER_PASSWORD unset; skipping.")
            return

        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write(f"Superuser {email} already exists; skipping.")
            return

        User.objects.create_superuser(email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created bootstrap superuser {email}."))
