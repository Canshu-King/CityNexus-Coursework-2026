"""Mature iCalendar extraction for Journey Inbox imports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from icalendar import Calendar


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    decoded = value.dt if hasattr(value, "dt") else value
    if isinstance(decoded, datetime):
        if decoded.tzinfo is None:
            decoded = decoded.replace(tzinfo=timezone.utc)
        return decoded.isoformat()
    if isinstance(decoded, date):
        return datetime(decoded.year, decoded.month, decoded.day, tzinfo=timezone.utc).isoformat()
    return None


def parse_reservation_calendar(text: str, *, default_type: str = "RAIL") -> dict[str, Any]:
    calendar = Calendar.from_ical(text)
    event = next((component for component in calendar.walk() if component.name == "VEVENT"), None)
    if event is None:
        raise ValueError("ICS payload does not contain a VEVENT")
    values = {
        "reservation_type": default_type,
        "confirmation_ref": str(event.get("UID")) if event.get("UID") else None,
        "destination": {"name": str(event.get("LOCATION"))} if event.get("LOCATION") else None,
        "scheduled_departure_at": _iso(event.get("DTSTART")),
        "scheduled_arrival_at": _iso(event.get("DTEND")),
        "summary": str(event.get("SUMMARY")) if event.get("SUMMARY") else "Imported itinerary",
        "provider_key": "ics-import",
    }
    values["field_provenance"] = {
        key: {"source": "ICS", "status": "EXTRACTED" if value not in (None, {}) else "UNKNOWN"}
        for key, value in values.items()
        if key != "field_provenance"
    }
    return values
