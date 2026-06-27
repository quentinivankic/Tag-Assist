"""Read EXIF GPS + capture date from a photo to pre-fill the interview.

Kept dependency-light: only Pillow. Reverse-geocoding GPS coordinates into a
place name is left to the optional ``reverse_geocoder`` package (see Roadmap);
here we surface raw coordinates and the capture year, which already let the UI
pre-suggest a date tag and hint that location data exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image, ExifTags
    _PIL_OK = True
except Exception:  # pragma: no cover - Pillow is a core dep, but be safe
    _PIL_OK = False

_GPS_IFD = 0x8825  # ExifTags.IFD.GPSInfo


@dataclass
class PhotoMeta:
    year: int | None = None
    date_taken: str | None = None  # "YYYY:MM:DD HH:MM:SS" as stored
    gps: tuple[float, float] | None = None  # (lat, lon) in decimal degrees

    @property
    def has_gps(self) -> bool:
        return self.gps is not None

    def suggested_context_tags(self) -> list[str]:
        """Cheap auto-suggestions derived purely from metadata."""
        tags: list[str] = []
        if self.year:
            tags.append(str(self.year))
        return tags


def _to_degrees(value) -> float:
    """Convert EXIF GPS rational (d, m, s) to decimal degrees."""
    d, m, s = (float(x) for x in value)
    return d + m / 60.0 + s / 3600.0


def read_meta(path: str | Path) -> PhotoMeta:
    """Extract capture date + GPS from a photo. Never raises on bad input."""
    meta = PhotoMeta()
    if not _PIL_OK:
        return meta
    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return meta
    if not exif:
        return meta

    # Capture date: DateTimeOriginal (0x9003) falls back to DateTime (0x0132).
    raw_date = exif.get(0x9003) or exif.get(0x0132)
    if isinstance(raw_date, str) and len(raw_date) >= 4 and raw_date[:4].isdigit():
        meta.date_taken = raw_date
        meta.year = int(raw_date[:4])

    # GPS lives in a sub-IFD.
    try:
        gps = exif.get_ifd(_GPS_IFD)
    except Exception:
        gps = None
    if gps:
        lat = gps.get(2)
        lat_ref = gps.get(1)
        lon = gps.get(4)
        lon_ref = gps.get(3)
        if lat and lon and lat_ref and lon_ref:
            try:
                lat_d = _to_degrees(lat)
                lon_d = _to_degrees(lon)
                if str(lat_ref).upper().startswith("S"):
                    lat_d = -lat_d
                if str(lon_ref).upper().startswith("W"):
                    lon_d = -lon_d
                meta.gps = (round(lat_d, 6), round(lon_d, 6))
            except Exception:
                pass
    return meta
