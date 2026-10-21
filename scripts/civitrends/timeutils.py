"""Timezone and date-boundary helpers for collection and exports."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


JST = timezone(timedelta(hours=9), name="JST")


def parse_timezone_offset(value: str) -> timezone:
    if value in ("Z", "UTC", "+00:00"):
        return timezone.utc
    if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
        raise ValueError(f"invalid timezone offset: {value}")
    try:
        hours = int(value[1:3])
        minutes = int(value[4:6])
    except ValueError as exc:
        raise ValueError(f"invalid timezone offset: {value}") from exc
    if hours > 23 or minutes > 59:
        raise ValueError(f"invalid timezone offset: {value}")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if value[0] == "+" else -delta)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def local_date(value: str, tz: timezone = JST) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date().isoformat()


def coverage_bounds(start_date: str, end_date: str, tz: timezone) -> tuple[str, str]:
    start_day = date.fromisoformat(start_date)
    end_day = date.fromisoformat(end_date)
    if start_day > end_day:
        raise ValueError("coverage start date must not be after end date")
    start = datetime.combine(start_day, time.min, tzinfo=tz)
    end = datetime.combine(end_day, time.max, tzinfo=tz)
    return (
        start.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
    )
