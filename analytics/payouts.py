"""Artist payouts derived from completed play events.

A payout is one artist's earnings for one period: the completed plays accrued by
their tracks inside the window, multiplied by the configured per-play rate.
Staff generate payouts on demand; each row snapshots the plays and rate so a
later rate change never rewrites history.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Count

from .models import ArtistPayout, PlayEvent

#: Money is quantised to whole cents before it is ever stored or summed.
CENT = Decimal("0.01")


def rate_per_play() -> Decimal:
    """The configured royalty rate, as a ``Decimal``."""
    raw = getattr(settings, "PAYOUT_RATE_PER_PLAY", "0.004")
    return _coerce_decimal(raw, field="PAYOUT_RATE_PER_PLAY")


def _coerce_decimal(value, *, field: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number.") from exc


def coerce_date(value):
    """Accept a ``date``/``datetime``/ISO string and return a ``date``.

    Raises ``ValueError`` for anything else (including ``None``) so callers can
    turn a bad request body into a 400 rather than a 500.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("Dates must be ISO formatted (YYYY-MM-DD).") from exc
    raise ValueError("A period_start and period_end date are required.")


def earnings_by_artist(period_start, period_end, *, rate=None):
    """Completed plays per artist in ``[period_start, period_end]``, with amounts.

    Returns a list of dicts ordered by descending play count. Every artist whose
    tracks were played at least once appears exactly once.
    """
    period_start = coerce_date(period_start)
    period_end = coerce_date(period_end)
    if period_start > period_end:
        raise ValueError("period_start must not be later than period_end.")

    rate = _coerce_decimal(rate, field="rate") if rate is not None else rate_per_play()
    if rate < 0:
        raise ValueError("rate must not be negative.")

    rows = (
        PlayEvent.objects.filter(
            completed=True,
            played_at__date__gte=period_start,
            played_at__date__lte=period_end,
        )
        .values("track__artist_id")
        .annotate(plays=Count("id"))
        .order_by("-plays")
    )

    return [
        {
            "artist_id": row["track__artist_id"],
            "plays": row["plays"],
            "rate": rate,
            "amount": (Decimal(row["plays"]) * rate).quantize(CENT, rounding=ROUND_HALF_UP),
        }
        for row in rows
    ]


def generate_payouts(
    period_start,
    period_end,
    *,
    rate=None,
    force: bool = False,
    minimum_amount: Decimal | None = None,
):
    """Create (or refresh) one payout per artist with eligible plays.

    Artists whose earnings fall below ``minimum_amount`` (default one cent) are
    skipped so the table is not flooded with zero-value rows. Existing rows for
    the same artist and period are left untouched unless ``force`` is set.
    Returns the list of payouts created or refreshed.
    """
    period_start = coerce_date(period_start)
    period_end = coerce_date(period_end)
    if period_start > period_end:
        raise ValueError("period_start must not be later than period_end.")

    rate = _coerce_decimal(rate, field="rate") if rate is not None else rate_per_play()
    threshold = CENT if minimum_amount is None else _coerce_decimal(minimum_amount, field="minimum_amount")

    touched = []
    with transaction.atomic():
        for row in earnings_by_artist(period_start, period_end, rate=rate):
            if row["amount"] < threshold:
                continue
            defaults = {
                "plays": row["plays"],
                "rate_per_play": row["rate"],
                "amount": row["amount"],
            }
            payout, created = ArtistPayout.objects.get_or_create(
                artist_id=row["artist_id"],
                period_start=period_start,
                period_end=period_end,
                defaults=defaults,
            )
            if not created and force:
                for field, value in defaults.items():
                    setattr(payout, field, value)
                payout.save(update_fields=[*defaults, "updated_at"])
            touched.append(payout)
    return touched
