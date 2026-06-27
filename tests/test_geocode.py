import pytest

from tagassist import geocode

pytestmark = pytest.mark.skipif(
    not geocode.available(), reason="reverse_geocoder not installed"
)


def test_reverse_phoenix():
    g = geocode.reverse(33.4484, -112.0740)  # downtown Phoenix
    assert g is not None
    assert g["state"] == "Arizona"
    assert g["country"] == "USA"
    assert g["city"]  # Phoenix or an adjacent municipality


def test_reverse_handles_garbage():
    # Out-of-range / nonsense coords must not raise.
    assert geocode.reverse(999, 999) is not None or True
