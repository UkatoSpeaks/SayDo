"""What part of the company a claim or an actual is about.

- ``company``: the whole company (the default).
- ``services``: the services business, for companies that guide it separately from the
  total (HCLTech guides "services revenue growth" apart from company revenue growth).
- ``segment:<name>``: a named segment, vertical or revenue line ("segment:software").

Claims with different scopes are never linked into one thread, and a claim is only
verified against actuals of its own scope.
"""

import re

COMPANY = "company"
SERVICES = "services"
SEGMENT_PREFIX = "segment:"
MAX_SCOPE_CHARS = 64

_COMPANY_WORDS = frozenset(
    {"", "company", "total", "overall", "consolidated", "group", "firm", "null", "none"}
)
_SERVICES_WORDS = frozenset({"services", "service", "it services", "services business"})


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9&]+", "-", name.lower()).strip("-")
    return slug[: MAX_SCOPE_CHARS - len(SEGMENT_PREFIX)]


def normalize_scope(raw: str | None) -> str:
    """``company``, ``services`` or ``segment:<slug>`` from whatever the model or a CSV
    wrote ("Company", "segment: HCL Software", "services")."""
    text = " ".join((raw or "").strip().lower().split())
    if text.startswith(SEGMENT_PREFIX):
        name = text[len(SEGMENT_PREFIX) :].strip()
        if name in _SERVICES_WORDS:
            return SERVICES
        slug = _slug(name)
        return f"{SEGMENT_PREFIX}{slug}" if slug else COMPANY
    if text in _COMPANY_WORDS:
        return COMPANY
    if text in _SERVICES_WORDS:
        return SERVICES
    slug = _slug(text)
    return f"{SEGMENT_PREFIX}{slug}" if slug else COMPANY


def scope_label(scope: str | None) -> str | None:
    """Text put before a metric label: None for the company, "Services", "Software
    segment"."""
    if not scope or scope == COMPANY:
        return None
    if scope == SERVICES:
        return "Services"
    name = scope.removeprefix(SEGMENT_PREFIX).replace("-", " ")
    return f"{name[:1].upper()}{name[1:]} segment"
