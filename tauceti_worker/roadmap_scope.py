"""Parsing shared by roadmap selection, maintenance and authoring limits."""


def roadmap_selection(value: str | None) -> tuple[str, ...]:
    """Trim comma-separated areas, dropping empty entries and preserving first occurrence order."""
    return tuple(dict.fromkeys(area for token in (value or "").split(",") if (area := token.strip())))
