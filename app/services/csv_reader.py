"""
CSV-Reader und Validierungskomponente für Order-Dateien.

Liest die täglichen Orders-CSV-Dateien ein, extrahiert die Leg-Strukturen und
validiert die Integrität und Konsistenz von Bracket-Order-Gruppen.
"""

import csv
from decimal import Decimal
from pathlib import Path

import structlog

from app.core.models import LegRow

logger = structlog.get_logger()


def validate_group(group_id: str, legs: list[LegRow]) -> tuple[bool, str]:
    """
    Validiert eine Gruppe von Order-Legs (Bracket-Gruppe).
    Gibt (True, "") bei Erfolg zurück, andernfalls (False, Fehlermeldung).
    """
    if not legs:
        return False, "Gruppe enthält keine Legs"

    is_valid, msg = _check_group_structure(legs)
    if not is_valid:
        return False, msg

    is_valid, msg = _validate_leg_attributes(legs)
    if not is_valid:
        return False, msg

    return True, ""


def _check_group_structure(legs: list[LegRow]) -> tuple[bool, str]:
    entries = [leg for leg in legs if leg.bracket_role == "ENTRY"]
    if len(entries) > 1:
        return (
            False,
            f"Gruppe darf maximal eine ENTRY-Order enthalten (gefunden: {len(entries)})",
        )
    if len(entries) == 0:
        exit_legs = [leg for leg in legs if leg.bracket_role in ("SL", "TP", "EXIT")]
        if not exit_legs:
            return (
                False,
                "Gruppe muss entweder eine ENTRY-Order oder mindestens eine Exit-Order (SL, TP, EXIT) enthalten",
            )

    if entries:
        entry = entries[0]
        entry_action = entry.action
        exit_action = "SELL" if entry_action == "BUY" else "BUY"
        for leg in legs:
            if leg.bracket_role in ("SL", "TP", "EXIT") and leg.action != exit_action:
                return (
                    False,
                    f"Exit-Leg {leg.bracket_role} muss Gegenrichtung ({exit_action}) zu ENTRY ({entry_action}) sein",
                )

    return True, ""


def _validate_leg_attributes(legs: list[LegRow]) -> tuple[bool, str]:
    symbol = legs[0].symbol
    account_id = legs[0].account_id

    for leg in legs:
        if leg.symbol != symbol:
            return (
                False,
                f"Legs haben unterschiedliche Symbole: {leg.symbol} vs {symbol}",
            )
        if leg.account_id != account_id:
            return (
                False,
                f"Legs haben unterschiedliche Account-IDs: {leg.account_id} vs {account_id}",
            )

        if leg.sec_type != "STK":
            return (
                False,
                f"Ausschliesslich sec_type='STK' ist erlaubt (gefunden: {leg.sec_type})",
            )
        if leg.exchange != "SMART":
            return (
                False,
                f"Ausschliesslich exchange='SMART' ist erlaubt (gefunden: {leg.exchange})",
            )

        if leg.action not in ("BUY", "SELL"):
            return False, f"Ungueltige Aktion: {leg.action}"

        if leg.bracket_role not in ("ENTRY", "SL", "TP", "EXIT"):
            return False, f"Ungueltige bracket_role: {leg.bracket_role}"

        if leg.quantity <= 0:
            return False, f"Menge muss groesser als 0 sein (gefunden: {leg.quantity})"

        if leg.order_type in ("LMT", "STP"):
            if leg.target_price is None or leg.target_price <= Decimal("0.0"):
                return (
                    False,
                    f"target_price ist fuer order_type='{leg.order_type}' zwingend erforderlich",
                )

    return True, ""


def load_csv(csv_path: Path) -> dict[str, list[LegRow]]:
    """
    Liest die CSV-Datei ein und gruppiert die Eintraege nach trade_group_id.
    """
    grouped_legs: dict[str, list[LegRow]] = {}

    if not csv_path.exists():
        logger.error("CSV file does not exist", path=str(csv_path))
        return grouped_legs

    try:
        with open(csv_path, encoding="utf-8-sig") as file_handle:
            reader = csv.DictReader(file_handle)
            reader.fieldnames = (
                [name.strip() for name in reader.fieldnames]
                if reader.fieldnames
                else []
            )

            for row_number, row in enumerate(reader, start=2):
                try:
                    price_str = row.get("target_price") or ""
                    target_price = (
                        Decimal(price_str.strip()) if price_str.strip() else None
                    )

                    leg = LegRow(
                        trade_group_id=row["trade_group_id"].strip(),
                        bracket_role=row["bracket_role"].strip().upper(),
                        symbol=row["symbol"].strip().upper(),
                        sec_type=row["sec_type"].strip().upper(),
                        exchange=row["exchange"].strip().upper(),
                        account_id=row["account_id"].strip(),
                        action=row["action"].strip().upper(),
                        quantity=int(row["quantity"].strip()),
                        order_type=row["order_type"].strip().upper(),
                        target_price=target_price,
                        tif=row.get("tif", "GTC").strip().upper(),
                        strategy_name=row.get("strategy_name", "").strip(),
                    )

                    grouped_legs.setdefault(leg.trade_group_id, []).append(leg)

                except KeyError as key_error:
                    logger.error(
                        "Missing column in CSV row",
                        row_number=row_number,
                        error=str(key_error),
                    )
                except ValueError as value_error:
                    logger.error(
                        "Invalid data format in CSV row",
                        row_number=row_number,
                        error=str(value_error),
                    )

    except Exception as exception:
        logger.error("Error reading CSV file", error=str(exception))

    return grouped_legs
