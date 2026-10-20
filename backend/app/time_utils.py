"""Timestamps in API responses are always sent as explicit UTC.

SQLite returns stored datetimes without a timezone even though the app writes UTC.
Serialised as-is ("2026-10-20T03:24:22") a browser reads them as *local* time, so in
India every time showed 5½ hours early. ``UtcDateTime`` treats a naive value as UTC
and serialises it with an offset ("2026-10-20T03:24:22Z" style), which browsers
convert to the viewer's local time correctly.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(as_utc)]
