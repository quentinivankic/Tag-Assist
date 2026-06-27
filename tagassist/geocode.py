"""Offline reverse-geocoding: GPS coordinates -> nearest place name.

Uses the optional ``reverse_geocode`` package, which ships a bundled city
database (no network, no API keys, pure wheel — fits the self-hosted goal).
Accuracy is city/town level: a coordinate in South Mountain resolves to
"Phoenix", not the park itself. That's intentional — Tag-Assist uses it to get
you to the city, and you pick the exact spot (which may be a learned child like
"Moms House").

If ``reverse_geocode`` isn't installed, ``reverse()`` returns None and the app
falls back to showing raw coordinates.
"""

from __future__ import annotations

try:
    import reverse_geocode as _rg
    _RG_OK = True
except Exception:  # pragma: no cover - optional dependency
    _RG_OK = False

# Map a few country codes to the names people actually tag with; otherwise the
# package's full country name is used (e.g. 'Denmark').
_COUNTRY = {
    "US": "USA",
    "GB": "UK",
}


def available() -> bool:
    return _RG_OK


def reverse(lat: float, lon: float) -> dict | None:
    """Return {'city', 'state', 'country'} for a coordinate, or None.

    ``state`` is the full admin name (e.g. 'Arizona'); ``country`` is mapped to a
    short name where known (US -> USA), else the full name (e.g. 'Denmark').
    """
    if not _RG_OK:
        return None
    try:
        results = _rg.search([(float(lat), float(lon))])
    except Exception:
        return None
    if not results:
        return None
    r = results[0]
    cc = (r.get("country_code") or "").upper()
    return {
        "city": (r.get("city") or "").strip() or None,
        "state": (r.get("state") or "").strip() or None,
        "country": _COUNTRY.get(cc, r.get("country")) or None,
    }
