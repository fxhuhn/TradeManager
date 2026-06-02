import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal

import aiosqlite
import structlog

from app.services.notifier import TelegramNotifier

logger = structlog.get_logger()

# In-Memory-Verzeichnis zur Vermeidung paralleler Settlement-Berechnungen
SETTLEMENT_LOCKS: dict[str, asyncio.Lock] = {}
SETTLEMENT_LOCKS_LOCK = asyncio.Lock()


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


async def trigger_settlement(
    database_connection_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    trade_group_id: str,
    account_id: str,
    notifier: TelegramNotifier,
) -> None:
    """
    Berechnet nach der Schließung eines Trades (Exit-Order auf 'Filled')
    das PnL und Slippage und schreibt das Ergebnis atomar in trades_settlement.
    Nutzt das lock-basierte Pattern gegen Race Conditions.
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

            entry_executions: list[tuple[Decimal, Decimal]] = []
            exit_executions: list[tuple[Decimal, Decimal]] = []
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
                        entry_executions.append((quantity, price))
                        entry_target_price = (
                            Decimal(str(row["target_price"]))
                            if row["target_price"] is not None
                            else Decimal("0.0")
                        )
                        entry_action = row["action"]
                    elif role in ("SL", "TP", "EXIT"):
                        exit_executions.append((quantity, price))

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

            # 4. VWAP-Berechnungen durchführen (hochpräzise mit Decimal)
            # VWAP = Summe(Menge_i * Preis_i) / Summe(Menge_i)
            entry_sum_quantity = sum(quantity for quantity, _ in entry_executions)
            entry_sum_value = sum(
                quantity * price for quantity, price in entry_executions
            )
            avg_entry_price = (
                entry_sum_value / entry_sum_quantity
                if entry_sum_quantity > Decimal("0.0")
                else Decimal("0.0")
            )

            exit_sum_quantity = sum(quantity for quantity, _ in exit_executions)
            exit_sum_value = sum(
                quantity * price for quantity, price in exit_executions
            )
            avg_exit_price = (
                exit_sum_value / exit_sum_quantity
                if exit_sum_quantity > Decimal("0.0")
                else Decimal("0.0")
            )

            # 5. Slippage berechnen
            # (positiv = günstiger gefüllt als geplant)
            if entry_action == "BUY":
                price_diff_slippage = entry_target_price - avg_entry_price
            else:
                price_diff_slippage = avg_entry_price - entry_target_price

            # 6. PnL-Berechnung
            # net_pnl = (direction * (avg_exit - avg_entry) * total_qty) - total_commissions
            direction = Decimal("1") if entry_action == "BUY" else Decimal("-1")
            total_quantity = (
                entry_sum_quantity  # Wir nehmen die gefüllte Entry-Menge als Basis
            )

            gross_profit_loss = (
                direction * (avg_exit_price - avg_entry_price) * total_quantity
            )
            net_profit_loss = gross_profit_loss - total_commissions

            logger.info(
                "Settlement-Berechnung abgeschlossen",
                trade_group_id=trade_group_id,
                entry_vwap=float(avg_entry_price),
                exit_vwap=float(avg_exit_price),
                slippage=float(price_diff_slippage),
                commissions=float(total_commissions),
                net_pnl=float(net_profit_loss),
            )

            # 7. Ergebnisse atomar in trades_settlement schreiben
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
                        float(avg_entry_price),
                        float(avg_exit_price),
                        float(price_diff_slippage),
                        float(total_commissions),
                        float(net_profit_loss),
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
                "🟢 Profit" if net_profit_loss >= Decimal("0.0") else "🔴 Verlust"
            )
            await notifier.send_message(
                f"✅ TRADE SETTLEMENT ({trade_group_id})\n"
                f"• Symbol: {entry_action} Position\n"
                f"• Entry VWAP: {float(avg_entry_price):.2f} (Target: {float(entry_target_price):.2f})\n"
                f"• Exit VWAP: {float(avg_exit_price):.2f}\n"
                f"• Slippage: {float(price_diff_slippage):+.4f}\n"
                f"• Gebühren: {float(total_commissions):.2f} USD\n"
                f"• *Netto-PnL:* {float(net_profit_loss):+.2f} USD ({profit_loss_emoji})"
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
