"""RFC 3339 §5.6 ``date-time`` parsing and formatting."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

__all__ = ["format_rfc3339", "parse_rfc3339"]

_DATE_TIME: Final = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})[Tt]"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})(?P<fraction>\.[0-9]+)?"
    r"(?P<offset>[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 ``date-time`` into an aware datetime.

    Field ranges are checked (including days per month). A leap second
    (``:60``) is accepted only at 23:59 UTC-equivalent minute boundaries and
    is represented as the following second.

    Raises:
        ValueError: not an RFC 3339 date-time.
    """
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        raise ValueError("not an RFC 3339 date-time")
    year, month, day = int(match["year"]), int(match["month"]), int(match["day"])
    hour, minute, second = int(match["hour"]), int(match["minute"]), int(match["second"])
    if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        raise ValueError("date out of range")
    if hour > 23 or minute > 59 or second > 60:
        raise ValueError("time out of range")
    offset_text = match["offset"]
    if offset_text in ("Z", "z"):
        tz: timezone = UTC
    else:
        hours, minutes = int(offset_text[1:3]), int(offset_text[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("offset out of range")
        delta = timedelta(hours=hours, minutes=minutes)
        tz = timezone(-delta if offset_text[0] == "-" else delta)
    microsecond = 0
    if match["fraction"]:
        microsecond = int((match["fraction"][1:] + "000000")[:6])
    leap = second == 60
    result = datetime(
        year, month, day, hour, minute, 59 if leap else second, microsecond, tzinfo=tz
    )
    if leap:
        utc = result.astimezone(UTC)
        if (utc.hour, utc.minute) != (23, 59):
            raise ValueError("leap seconds occur only at the end of a UTC day")
        result += timedelta(seconds=1)
    return result


def format_rfc3339(value: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC (fractions dropped)."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
