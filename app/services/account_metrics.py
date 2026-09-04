"""Dienst zur Erfassung, Persistierung und Abfrage von Kontometriken.

Verwaltet Kontokennzahlen wie Net Liquidation (Equity), Maintenance Margin,
verfügbare Mittel, Cushion und Kaufkraft. Speichert Snapshots in der SQLite-Datenbank.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import aiosqlite
import structlog
from ib_async import IB

from app.core.db import transaction

logger = structlog.get_logger()


@dataclass(frozen=True)
class AccountMetricsSnapshot:
    """Vollständige Kontokennzahlen für Monitoring und Statusbericht."""

    account_id: str
    net_liquidation: Decimal
    total_cash_value: Decimal
    available_funds: Decimal
    maint_margin_req: Decimal
    cushion_pct: Decimal
    buying_power: Decimal
    updated_at: str = ""


async def save_account_metrics(
    database_connection: aiosqlite.Connection,
    account_id: str,
    metrics: AccountMetricsSnapshot,
) -> None:
    """Persistiert die Kontokennzahlen atomar in der SQLite-Tabelle account_metrics.

    Args:
        database_connection: Aktive SQLite-Verbindung.
        account_id: Kontonummer bei Interactive Brokers.
        metrics: Zu speichernde Kontokennzahlen.
    """
    async with transaction(database_connection):
        await database_connection.execute(
            """
            INSERT INTO account_metrics (
                account_id,
                net_liquidation,
                total_cash_value,
                available_funds,
                maint_margin_req,
                cushion_pct,
                buying_power,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(account_id) DO UPDATE SET
                net_liquidation = excluded.net_liquidation,
                total_cash_value = excluded.total_cash_value,
                available_funds = excluded.available_funds,
                maint_margin_req = excluded.maint_margin_req,
                cushion_pct = excluded.cushion_pct,
                buying_power = excluded.buying_power,
                updated_at = datetime('now', 'localtime');
            """,
            (
                account_id,
                float(metrics.net_liquidation),
                float(metrics.total_cash_value),
                float(metrics.available_funds),
                float(metrics.maint_margin_req),
                float(metrics.cushion_pct),
                float(metrics.buying_power),
            ),
        )
    logger.info(
        "Account metrics snapshot persisted",
        account_id=account_id,
        net_liquidation=float(metrics.net_liquidation),
        cushion_pct=float(metrics.cushion_pct),
        available_funds=float(metrics.available_funds),
    )


async def get_latest_account_metrics(
    database_connection: aiosqlite.Connection,
    account_id: str | None = None,
) -> AccountMetricsSnapshot | None:
    """Liest den neuesten gespeicherten Kontokennzahlen-Snapshot aus der Datenbank.

    Args:
        database_connection: Aktive SQLite-Verbindung.
        account_id: Optionale spezifische Kontonummer.

    Returns:
        AccountMetricsSnapshot falls vorhanden, sonst None.
    """
    query = """
        SELECT account_id, net_liquidation, total_cash_value, available_funds,
               maint_margin_req, cushion_pct, buying_power, updated_at
        FROM account_metrics
    """
    parameters: tuple[object, ...] = ()
    if account_id:
        query += " WHERE account_id = ?"
        parameters = (account_id,)
    query += " ORDER BY updated_at DESC LIMIT 1"

    try:
        async with database_connection.execute(query, parameters) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return AccountMetricsSnapshot(
                account_id=str(row["account_id"]),
                net_liquidation=Decimal(str(row["net_liquidation"])),
                total_cash_value=Decimal(str(row["total_cash_value"])),
                available_funds=Decimal(str(row["available_funds"])),
                maint_margin_req=Decimal(str(row["maint_margin_req"])),
                cushion_pct=Decimal(str(row["cushion_pct"])),
                buying_power=Decimal(str(row["buying_power"])),
                updated_at=str(row["updated_at"]),
            )
    except Exception as exception:
        logger.debug(
            "Unable to read account metrics from database",
            error=str(exception),
        )
        return None


async def sync_and_save_account_metrics(
    interactive_brokers: IB,
    account_id: str,
    database_connection: aiosqlite.Connection,
) -> AccountMetricsSnapshot | None:
    """Fragt Kontowerte von IBKR ab und speichert sie in der Datenbank.

    Args:
        interactive_brokers: IB-Verbindungsinstanz.
        account_id: Kontonummer bei Interactive Brokers.
        database_connection: Aktive SQLite-Verbindung.

    Returns:
        Gespeicherter AccountMetricsSnapshot oder None im Fehlerfall.
    """
    from app.services.importer import fetch_account_balance_metrics

    try:
        balance_metrics = await fetch_account_balance_metrics(
            interactive_brokers, account_id
        )
        snapshot = AccountMetricsSnapshot(
            account_id=account_id,
            net_liquidation=balance_metrics.net_liquidation_value,
            total_cash_value=balance_metrics.total_cash_value,
            available_funds=balance_metrics.available_funds_value,
            maint_margin_req=balance_metrics.maint_margin_req,
            cushion_pct=balance_metrics.cushion_pct,
            buying_power=balance_metrics.buying_power,
        )
        await save_account_metrics(database_connection, account_id, snapshot)
        return snapshot
    except Exception as exception:
        logger.warning(
            "Failed to sync and save account metrics",
            account_id=account_id,
            error=str(exception),
        )
        return None
