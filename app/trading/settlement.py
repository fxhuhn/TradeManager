"""
Abwicklung (Settlement) von geschlossenen Trades.

Berechnet nach der Schließung eines Trades (ENTRY und EXIT vollzogen)
das PnL und die Slippage hochpräzise und speichert die Ergebnisse in der Datenbank.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

import aiosqlite
import structlog

from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()

# In-Memory-Verzeichnis zur Vermeidung paralleler Settlement-Berechnungen
SETTLEMENT_LOCKS: dict[str, asyncio.Lock] = {}
SETTLEMENT_LOCKS_LOCK = asyncio.Lock()


async def trigger_settlement(
    database_connection_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    trade_group_id: str,
    account_id: str,
    notifier: TelegramNotifier,
) -> None:
    """
    Orchestriert das Settlement einer bestimmten Trade-Gruppe.

    Lädt die Ausführungsdaten aus der Datenbank, delegiert die Berechnung
    an den Functional Core (calculate_settlement) und persistiert die Ergebnisse.
    """
    lock = await get_settlement_lock(trade_group_id)

    # 1. Lock erwerben
    async with lock:
        db = await database_connection_factory()
        try:
            # 2. Prüfen, ob bereits ein Settlement-Eintrag existiert
            async with db.execute(
                "SELECT trade_group_id FROM trades_settlement WHERE account_id = ? AND trade_group_id = ?",
                (account_id, trade_group_id),
            ) as cursor:
                if await cursor.fetchone():
                    logger.info(
                        "Settlement fuer Trade-Gruppe bereits vorhanden. Abbrechen.",
                        trade_group_id=trade_group_id,
                    )
                    return

            # 3. Alle Executions für diese Trade-Gruppe abfragen
            query = """
                SELECT e.qty, e.price, COALESCE(e.commission, 0.0) as commission,
                       o.bracket_role, o.action, o.target_price
                FROM executions e
                JOIN orders o ON e.order_id = o.order_id
                WHERE o.trade_group_id = ?
            """

            entry_executions: list[ExecutionTuple] = []
            exit_executions: list[ExecutionTuple] = []
            total_commissions = Decimal("0.0")
            entry_target_price = Decimal("0.0")
            entry_action = "BUY"

            async with db.execute(query, (trade_group_id,)) as cursor:
                async for row in cursor:
                    role = row["bracket_role"]
                    quantity = Decimal(str(row["qty"]))
                    price = Decimal(str(row["price"]))
                    commission = (
                        Decimal(str(row["commission"]))
                        if row["commission"] is not None
                        else Decimal("0.0")
                    )

                    total_commissions += commission

                    if role == "ENTRY":
                        entry_executions.append(
                            ExecutionTuple(quantity=quantity, price=price)
                        )
                        entry_target_price = (
                            Decimal(str(row["target_price"]))
                            if row["target_price"] is not None
                            else Decimal("0.0")
                        )
                        entry_action = row["action"]
                    elif role in ("SL", "TP", "EXIT"):
                        exit_executions.append(
                            ExecutionTuple(quantity=quantity, price=price)
                        )

            if not entry_executions:
                logger.warning(
                    "Keine ENTRY-Executions fuer Settlement gefunden",
                    trade_group_id=trade_group_id,
                )
                return
            if not exit_executions:
                logger.warning(
                    "Keine EXIT-Executions fuer Settlement gefunden (Trade eventuell noch offen)",
                    trade_group_id=trade_group_id,
                )
                return

            # 4. Berechnung an den Functional Core delegieren
            calculation_inputs = SettlementInput(
                entry_executions=entry_executions,
                exit_executions=exit_executions,
                entry_target_price=entry_target_price,
                entry_action=entry_action,
                total_commissions=total_commissions,
            )
            calculation_outputs = calculate_settlement(calculation_inputs)

            logger.info(
                "Settlement-Berechnung abgeschlossen",
                trade_group_id=trade_group_id,
                entry_vwap=float(calculation_outputs.avg_entry_price),
                exit_vwap=float(calculation_outputs.avg_exit_price),
                slippage=float(calculation_outputs.price_diff_slippage),
                commissions=float(total_commissions),
                net_pnl=float(calculation_outputs.net_profit_loss),
            )

            # 5. Ergebnisse atomar in die Datenbank schreiben
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO trades_settlement (
                        account_id, trade_group_id, avg_entry_price, avg_exit_price,
                        price_diff_slippage, total_commissions, net_pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        trade_group_id,
                        float(calculation_outputs.avg_entry_price),
                        float(calculation_outputs.avg_exit_price),
                        float(calculation_outputs.price_diff_slippage),
                        float(total_commissions),
                        float(calculation_outputs.net_profit_loss),
                    ),
                )
                await db.execute("COMMIT")
                logger.info(
                    "Settlement erfolgreich verbucht", trade_group_id=trade_group_id
                )
            except Exception as exception:
                await db.execute("ROLLBACK")
                logger.error(
                    "Fehler beim DB-Eintrag des Settlements",
                    trade_group_id=trade_group_id,
                    error=str(exception),
                )
                raise exception

            # Telegram-Meldung über erfolgreichen Trade-Abschluss senden
            profit_loss_emoji = (
                "🟢 Profit"
                if calculation_outputs.net_profit_loss >= Decimal("0.0")
                else "🔴 Verlust"
            )
            await notifier.send_message(
                f"✅ TRADE SETTLEMENT ({trade_group_id})\n"
                f"• Symbol: {entry_action} Position\n"
                f"• Entry: {float(calculation_outputs.avg_entry_price):.2f} (Target: {float(entry_target_price):.2f})\n"
                f"• Exit: {float(calculation_outputs.avg_exit_price):.2f}\n"
                f"• Slippage: {float(calculation_outputs.price_diff_slippage):+.2f}\n"
                f"• Gebühren: {float(total_commissions):.2f} USD\n"
                f"• *Netto-PnL:* {float(calculation_outputs.net_profit_loss):+.2f} USD ({profit_loss_emoji})"
            )

        except Exception as exception:
            logger.error(
                "Schwerer Fehler im Settlement-Prozess",
                trade_group_id=trade_group_id,
                error=str(exception),
            )
        finally:
            await db.close()

    # Lock-Bereinigung durchführen
    await cleanup_settlement_lock(trade_group_id)


async def get_settlement_lock(trade_group_id: str) -> asyncio.Lock:
    """Holt oder erzeugt ein asyncio.Lock für eine bestimmte Trade-Gruppe."""
    async with SETTLEMENT_LOCKS_LOCK:
        if trade_group_id not in SETTLEMENT_LOCKS:
            SETTLEMENT_LOCKS[trade_group_id] = asyncio.Lock()
        return SETTLEMENT_LOCKS[trade_group_id]


async def cleanup_settlement_lock(trade_group_id: str) -> None:
    """Entfernt das Lock aus dem Speicher zur Bereinigung."""
    async with SETTLEMENT_LOCKS_LOCK:
        SETTLEMENT_LOCKS.pop(trade_group_id, None)


@dataclass(frozen=True)
class ExecutionTuple:
    """Repräsentiert ein Ausführungs-Leg mit Stückzahl und Preis (Functional Core)."""

    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class SettlementInput:
    """Kapselt die Eingabedaten für die reine Settlement-Berechnung (Functional Core)."""

    entry_executions: list[ExecutionTuple]
    exit_executions: list[ExecutionTuple]
    entry_target_price: Decimal
    entry_action: str
    total_commissions: Decimal


@dataclass(frozen=True)
class SettlementOutput:
    """Kapselt das Ergebnis der reinen Settlement-Berechnung (Functional Core)."""

    avg_entry_price: Decimal
    avg_exit_price: Decimal
    price_diff_slippage: Decimal
    net_profit_loss: Decimal


def calculate_settlement(inputs: SettlementInput) -> SettlementOutput:
    """
    Führt die mathematische Settlement-Berechnung (VWAP, Slippage, PnL) durch.

    Diese Funktion ist pure: Sie enthält keinerlei Seiteneffekte (Datenbank, I/O, etc.).
    """
    entry_sum_quantity = sum(
        execution.quantity for execution in inputs.entry_executions
    )
    entry_sum_value = sum(
        execution.quantity * execution.price for execution in inputs.entry_executions
    )
    avg_entry_price = (
        entry_sum_value / entry_sum_quantity
        if entry_sum_quantity > Decimal("0.0")
        else Decimal("0.0")
    )

    exit_sum_quantity = sum(execution.quantity for execution in inputs.exit_executions)
    exit_sum_value = sum(
        execution.quantity * execution.price for execution in inputs.exit_executions
    )
    avg_exit_price = (
        exit_sum_value / exit_sum_quantity
        if exit_sum_quantity > Decimal("0.0")
        else Decimal("0.0")
    )

    if inputs.entry_action == "BUY":
        price_diff_slippage = inputs.entry_target_price - avg_entry_price
        direction = Decimal("1")
    else:
        price_diff_slippage = avg_entry_price - inputs.entry_target_price
        direction = Decimal("-1")

    gross_profit_loss = (
        direction * (avg_exit_price - avg_entry_price) * entry_sum_quantity
    )
    net_profit_loss = gross_profit_loss - inputs.total_commissions

    return SettlementOutput(
        avg_entry_price=avg_entry_price,
        avg_exit_price=avg_exit_price,
        price_diff_slippage=price_diff_slippage,
        net_profit_loss=net_profit_loss,
    )
