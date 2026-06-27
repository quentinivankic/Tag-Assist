import piexif
import pytest
from PIL import Image

from tagassist import exif

pytestmark = pytest.mark.skipif(
    not hasattr(exif, "_PIL_OK") or not exif._PIL_OK, reason="Pillow not available"
)


def test_read_meta_no_exif(tmp_path):
    p = tmp_path / "plain.jpg"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(p)
    meta = exif.read_meta(p)
    assert meta.year is None
    assert meta.has_gps is False
    assert meta.suggested_context_tags() == []


def test_read_meta_with_gps_and_date(tmp_path):
    p = tmp_path / "gps.jpg"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(p)
    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: b"N",
        piexif.GPSIFD.GPSLatitude: [(33, 1), (29, 1), (0, 1)],   # 33.4833 N
        piexif.GPSIFD.GPSLongitudeRef: b"W",
        piexif.GPSIFD.GPSLongitude: [(112, 1), (4, 1), (0, 1)],  # 112.0667 W
    }
    exif_dict = {
        "0th": {piexif.ImageIFD.DateTime: b"2023:07:15 18:30:00"},
        "GPS": gps_ifd,
    }
    piexif.insert(piexif.dump(exif_dict), str(p))

    meta = exif.read_meta(p)
    assert meta.year == 2023
    assert meta.has_gps is True
    lat, lon = meta.gps
    assert lat == pytest.approx(33.4833, abs=0.001)
    assert lon == pytest.approx(-112.0667, abs=0.001)
    assert "2023" in meta.suggested_context_tags()


def test_read_meta_bad_path():
    meta = exif.read_meta("/does/not/exist.jpg")
    assert meta.year is None and meta.has_gps is False
