"""
Unit-Tests für Hilfsfunktionen in app.core.models.
"""

from decimal import Decimal

import pytest

from app.core.models import decimal_from_db, parse_positive_decimal


def test_decimal_from_db() -> None:
    """Prüft die Konvertierung von DB-Werten in Decimal."""
    assert decimal_from_db(None) is None
    assert decimal_from_db("150.50") == Decimal("150.50")
    assert decimal_from_db(150.5) == Decimal("150.5")


@pytest.mark.parametrize(
    "input_value, expected",
    [
        (None, None),
        (0, None),
        (0.0, None),
        ("0.0", None),
        ("-10.5", None),
        (-5, None),
        ("invalid", None),
        ("150.50", Decimal("150.50")),
        (150.5, Decimal("150.5")),
        (Decimal("42.0"), Decimal("42.0")),
    ],
)
def test_parse_positive_decimal(input_value: object, expected: Decimal | None) -> None:
    """Prüft, dass nur positive Zahlen in Decimal umgewandelt werden, ansonsten None."""
    assert parse_positive_decimal(input_value) == expected
