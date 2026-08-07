# filename: tests/services/test_csv_reader.py
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.models import LegRow
from app.services.csv_reader import load_csv, validate_group


@pytest.fixture
def base_leg() -> LegRow:
    """Fixture providing a standard valid ENTRY LegRow."""
    return LegRow(
        trade_group_id="G123",
        bracket_role="ENTRY",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("150.00"),
        tif="GTC",
        strategy_name="Momentum",
    )


def test_validate_group_success_with_single_leg(base_leg: LegRow) -> None:
    """Verifies that validate_group succeeds with a single valid ENTRY leg."""
    # Arrange
    legs = [base_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is True
    assert error_message == ""


def test_validate_group_success_with_bracket_pair(base_leg: LegRow) -> None:
    """Verifies that validate_group succeeds with an ENTRY and opposing SL/TP legs."""
    # Arrange
    entry_leg = base_leg
    sl_leg = LegRow(
        trade_group_id="G123",
        bracket_role="SL",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="SELL",  # Opposing action
        quantity=100,
        order_type="STP",
        target_price=Decimal("140.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [entry_leg, sl_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is True
    assert error_message == ""


@pytest.mark.asyncio
async def test_csv_validation_futures_rejected() -> None:
    """FUT wird abgelehnt: Prüft, dass sec_type='FUT' korrekterweise abgewiesen wird."""
    invalid_legs = [
        LegRow(
            trade_group_id="20260530_Invalid",
            bracket_role="ENTRY",
            symbol="ES",
            sec_type="FUT",  # Ungültig
            exchange="SMART",
            account_id="DU12345",
            action="BUY",
            quantity=1,
            order_type="LMT",
            target_price=Decimal("5100.0"),
            tif="GTC",
            strategy_name="FuturesStrategy",
        )
    ]
    is_valid, error_message = validate_group("20260530_Invalid", invalid_legs)
    assert not is_valid
    assert "sec_type='STK' ist erlaubt" in error_message


@pytest.mark.asyncio
async def test_csv_validation_valid_bracket() -> None:
    """Validiert ein korrektes Bracket-Setup."""
    valid_legs = [
        LegRow(
            trade_group_id="20260530_Valid",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU12345",
            action="BUY",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("180.00"),
            tif="GTC",
            strategy_name="ValidBracket",
        ),
        LegRow(
            trade_group_id="20260530_Valid",
            bracket_role="SL",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU12345",
            action="SELL",
            quantity=100,
            order_type="STP",
            target_price=Decimal("175.00"),
            tif="GTC",
            strategy_name="ValidBracket",
        ),
        LegRow(
            trade_group_id="20260530_Valid",
            bracket_role="TP",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU12345",
            action="SELL",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("190.00"),
            tif="GTC",
            strategy_name="ValidBracket",
        ),
    ]
    is_valid, error_message = validate_group("20260530_Valid", valid_legs)
    assert is_valid
    assert error_message == ""


def test_validate_group_fails_on_empty_list() -> None:
    """Verifies that validate_group fails when the leg list is empty."""
    # Arrange
    legs = []

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "keine Legs" in error_message


def test_validate_group_fails_on_multiple_entries(base_leg: LegRow) -> None:
    """Verifies that validate_group fails when more than one ENTRY is present in the group."""
    # Arrange
    entry_one = base_leg
    entry_two = base_leg
    legs = [entry_one, entry_two]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "maximal eine ENTRY-Order" in error_message


def test_validate_group_fails_on_zero_entry_and_zero_exits(base_leg: LegRow) -> None:
    """Verifies that validate_group fails when there are no entry and no exit legs."""
    # Arrange
    invalid_leg = LegRow(
        trade_group_id="G123",
        bracket_role="INVALID",  # Neither ENTRY nor standard exit role
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="BUY",
        quantity=100,
        order_type="LMT",
        target_price=Decimal("150.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [invalid_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "entweder eine ENTRY-Order oder mindestens eine Exit-Order" in error_message


def test_validate_group_success_on_exit_only_group() -> None:
    """Verifies that validate_group succeeds on exit-only groups for existing positions."""
    # Arrange
    sl_leg = LegRow(
        trade_group_id="G123",
        bracket_role="SL",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="SELL",
        quantity=100,
        order_type="STP",
        target_price=Decimal("140.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [sl_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is True
    assert error_message == ""


def test_validate_group_fails_on_non_opposing_exit(base_leg: LegRow) -> None:
    """Verifies that exit legs must oppose the action of the ENTRY leg."""
    # Arrange
    entry_leg = base_leg
    sl_leg = LegRow(
        trade_group_id="G123",
        bracket_role="SL",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="BUY",  # Same direction as entry (BUY) -> Invalid
        quantity=100,
        order_type="STP",
        target_price=Decimal("140.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [entry_leg, sl_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "muss Gegenrichtung" in error_message


def test_validate_group_fails_on_mismatched_symbols(base_leg: LegRow) -> None:
    """Verifies that all legs within a group must share the same symbol."""
    # Arrange
    entry_leg = base_leg
    sl_leg = LegRow(
        trade_group_id="G123",
        bracket_role="SL",
        symbol="MSFT",  # Mismatched symbol
        sec_type="STK",
        exchange="SMART",
        account_id="DU123",
        action="SELL",
        quantity=100,
        order_type="STP",
        target_price=Decimal("140.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [entry_leg, sl_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "unterschiedliche Symbole" in error_message


def test_validate_group_fails_on_mismatched_accounts(base_leg: LegRow) -> None:
    """Verifies that all legs within a group must belong to the same account."""
    # Arrange
    entry_leg = base_leg
    sl_leg = LegRow(
        trade_group_id="G123",
        bracket_role="SL",
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        account_id="DU456",  # Mismatched account
        action="SELL",
        quantity=100,
        order_type="STP",
        target_price=Decimal("140.00"),
        tif="GTC",
        strategy_name="Momentum",
    )
    legs = [entry_leg, sl_leg]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "unterschiedliche Account-IDs" in error_message


def test_validate_group_fails_on_invalid_security_type(base_leg: LegRow) -> None:
    """Verifies that validate_group enforces STK as the only valid security type."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="OPT",  # Invalid type
            exchange="SMART",
            account_id="DU123",
            action="BUY",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "Ausschliesslich sec_type='STK' ist erlaubt" in error_message


def test_validate_group_fails_on_invalid_exchange(base_leg: LegRow) -> None:
    """Verifies that validate_group enforces SMART as the only valid exchange."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="NYSE",  # Invalid exchange
            account_id="DU123",
            action="BUY",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "Ausschliesslich exchange='SMART' ist erlaubt" in error_message


def test_validate_group_fails_on_invalid_action(base_leg: LegRow) -> None:
    """Verifies that validate_group fails on non-standard actions."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU123",
            action="HOLD",  # Invalid action
            quantity=100,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "Ungueltige Aktion" in error_message


def test_validate_group_fails_on_invalid_bracket_role(base_leg: LegRow) -> None:
    """Verifies that validate_group fails on invalid bracket roles."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="TRAILING_STOP",  # Invalid role
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU123",
            action="BUY",
            quantity=100,
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, _ = validate_group("G123", legs)

    # Assert
    assert is_valid is False


def test_validate_group_fails_on_zero_quantity(base_leg: LegRow) -> None:
    """Verifies that validate_group fails when order quantity is zero."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU123",
            action="BUY",
            quantity=0,  # Invalid quantity
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "Menge muss groesser als 0 sein" in error_message


def test_validate_group_fails_on_negative_quantity(base_leg: LegRow) -> None:
    """Verifies that validate_group fails when order quantity is negative."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU123",
            action="BUY",
            quantity=-50,  # Invalid quantity
            order_type="LMT",
            target_price=Decimal("150.00"),
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "Menge muss groesser als 0 sein" in error_message


@pytest.mark.parametrize(
    "order_type, target_price",
    [
        ("LMT", None),
        ("LMT", Decimal("0.0")),
        ("LMT", Decimal("-10.0")),
        ("STP", None),
        ("STP", Decimal("0.0")),
        ("STP", Decimal("-5.0")),
    ],
)
def test_validate_group_fails_on_missing_or_invalid_target_price_for_limit_and_stop_orders(
    base_leg: LegRow, order_type: str, target_price: Decimal | None
) -> None:
    """Verifies that limit and stop orders require a positive target price."""
    # Arrange
    legs = [
        LegRow(
            trade_group_id="G123",
            bracket_role="ENTRY",
            symbol="AAPL",
            sec_type="STK",
            exchange="SMART",
            account_id="DU123",
            action="BUY",
            quantity=100,
            order_type=order_type,
            target_price=target_price,
            tif="GTC",
            strategy_name="Momentum",
        )
    ]

    # Act
    is_valid, error_message = validate_group("G123", legs)

    # Assert
    assert is_valid is False
    assert "target_price ist fuer order_type" in error_message


def test_load_csv_non_existent_file() -> None:
    """Verifies that load_csv returns an empty dictionary when file does not exist."""
    # Arrange
    path = Path("non_existent_file_path_12345.csv")

    # Act
    grouped = load_csv(path)

    # Assert
    assert grouped == {}


def test_load_csv_valid_data(tmp_path: Path) -> None:
    """Verifies that load_csv parses correct CSV rows into group directories."""
    # Arrange
    csv_file = tmp_path / "orders.csv"
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "G1,ENTRY,AAPL,STK,SMART,DU123,BUY,100,LMT,150.00,GTC,Momentum\n"
        "G1,SL,AAPL,STK,SMART,DU123,SELL,100,STP,140.00,GTC,Momentum\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    # Act
    grouped = load_csv(csv_file)

    # Assert
    assert "G1" in grouped
    assert len(grouped["G1"]) == 2
    assert grouped["G1"][0].symbol == "AAPL"
    assert grouped["G1"][0].quantity == 100
    assert grouped["G1"][0].target_price == Decimal("150.00")
    assert grouped["G1"][1].bracket_role == "SL"


def test_load_csv_handles_missing_column(tmp_path: Path) -> None:
    """Verifies that load_csv skips rows containing missing required columns."""
    # Arrange
    csv_file = tmp_path / "orders.csv"
    # Missing 'symbol' column header in CSV
    csv_content = (
        "trade_group_id,bracket_role,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "G1,ENTRY,STK,SMART,DU123,BUY,100,LMT,150.00,GTC,Momentum\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    # Act
    grouped = load_csv(csv_file)

    # Assert
    assert grouped == {}  # The row was skipped due to KeyError on row['symbol']


def test_load_csv_handles_invalid_data_format(tmp_path: Path) -> None:
    """Verifies that load_csv skips rows containing malformed values."""
    # Arrange
    csv_file = tmp_path / "orders.csv"
    # Malformed quantity ('abc' instead of int)
    csv_content = (
        "trade_group_id,bracket_role,symbol,sec_type,exchange,account_id,action,quantity,order_type,target_price,tif,strategy_name\n"
        "G1,ENTRY,AAPL,STK,SMART,DU123,BUY,abc,LMT,150.00,GTC,Momentum\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    # Act
    grouped = load_csv(csv_file)

    # Assert
    assert grouped == {}  # Skipped due to ValueError on int() conversion
