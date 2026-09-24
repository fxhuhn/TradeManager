"""
Ausführungsworker (Execution Worker) für TWS-Auftragsplatzierungen.

Verarbeitet Trade-Gruppen asynchron aus einer Queue und sendet
die entsprechenden ENTRY und Child-Orders (SL, TP, EXIT) an die TWS.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import aiosqlite
import structlog
from ib_async import IB, Contract, Order, Trade

from app.core.config import Config
from app.core.db import transaction
from app.core.logging_setup import (
    TAG_CUSHION_ALERT,
    TAG_ORDER_REJECT,
    TAG_REAUTH_WAIT,
)
from app.core.models import OrderRow, order_row_from_db_row
from app.services.notifier import (
    BracketOrderDict,
    TelegramNotifier,
    build_tree_message,
)
from app.trading.error_codes import (
    is_market_closed_for_symbol,
    is_pre_market_hold_notice,
    is_read_only_error,
    is_reauthorization_error,
    is_trade_pre_market_held,
)
from app.trading.order_builder import (
    build_order,
    compute_loc_gtd_cutoff,
    extract_transmitted_price,
    is_past_loc_gtd_cutoff,
    make_contract_for_order,
    normalize_symbol,
    should_apply_loc_gtd,
)

logger = structlog.get_logger()

# Globales Lock zur Sicherung der Atomarität von getReqId() + DB-Write
ORDER_ID_LOCK = asyncio.Lock()


@dataclasses.dataclass(frozen=True)
class WorkerExecutionContext:
    """Immutable execution context aggregating database and service dependencies."""

    database: aiosqlite.Connection
    interactive_brokers: IB
    notifier: TelegramNotifier
    config: Config


def _format_submitted_orders_summary(
    placed_orders: Sequence[OrderRow],
) -> list[BracketOrderDict]:
    """
    Formats and sorts placed orders (ENTRY first, then TP, then SL, then EXIT) for Telegram notifications.

    Args:
        placed_orders: Sequence of successfully transmitted OrderRow records.

    Returns:
        List of BracketOrderDict dictionaries ordered by execution sequence.
    """
    order_dicts: list[BracketOrderDict] = [
        {
            "role": placed_order.bracket_role,
            "action": placed_order.action,
            "quantity": placed_order.quantity,
            "price": placed_order.target_price,
            "order_type": placed_order.order_type,
        }
        for placed_order in placed_orders
    ]
    role_priority: Final[dict[str, int]] = {"ENTRY": 0, "TP": 1, "SL": 2}
    order_dicts.sort(
        key=lambda order_dict: role_priority.get(str(order_dict.get("role", "")), 3)
    )
    return order_dicts


async def execution_worker(
    db_factory: Callable[[], Awaitable[aiosqlite.Connection]],
    interactive_brokers: IB,
    queue: asyncio.Queue[str],
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Asynchroner Execution Worker (Hintergrunddienst).

    Konsumiert permanent Trade-Gruppen-IDs aus der Queue und stößt
    deren Platzierung an.
    """
    logger.info("Starting Execution Worker background service")

    while True:
        trade_group_id = None
        try:
            trade_group_id = await queue.get()

            # Wenn die Verbindung getrennt ist, warten wir, bis sie wieder steht
            while not interactive_brokers.isConnected():
                logger.warning(
                    "Interactive Brokers not connected. Waiting for reconnection before placing order.",
                    trade_group_id=trade_group_id,
                )
                await asyncio.sleep(5.0)

            db = await db_factory()
            try:
                await process_trade_group(
                    db, interactive_brokers, trade_group_id, notifier, config
                )
            finally:
                await db.close()

            queue.task_done()

        except asyncio.CancelledError:
            logger.info("Execution Worker was cancelled.")
            raise
        except Exception as exception:
            logger.exception("Error in Execution Worker loop", error=str(exception))
            try:
                trade_group_identifier = (
                    trade_group_id if trade_group_id is not None else "Unbekannt"
                )
                worker_error_message = build_tree_message(
                    title="KRITISCHER FEHLER IM EXECUTION WORKER",
                    emoji="🚨",
                    rows=[
                        ("Trade-Gruppe", f"<code>{trade_group_identifier}</code>"),
                        ("Details", f"<i>{exception}</i>"),
                    ],
                )
                await notifier.send_message(worker_error_message)
            except Exception as telegram_exception:
                logger.critical(
                    "Failed to send Telegram error notification",
                    error=str(telegram_exception),
                )
            if trade_group_id is not None:
                queue.task_done()
            await asyncio.sleep(1.0)


def _split_trade_group_orders(
    orders: Sequence[OrderRow],
) -> tuple[OrderRow | None, list[OrderRow]]:
    """Splits a list of orders into the single ENTRY order and its associated child orders."""
    entry_order: OrderRow | None = None
    child_orders: list[OrderRow] = []
    for order in orders:
        if order.bracket_role == "ENTRY":
            entry_order = order
        else:
            child_orders.append(order)
    return entry_order, child_orders


async def _abort_remaining_trade_group_orders(
    db: aiosqlite.Connection, trade_group_id: str, new_status: str
) -> None:
    """Updates any remaining Created orders for the trade group to either Cancelled or Error."""
    async with transaction(db):
        await db.execute(
            "UPDATE orders SET status = ? WHERE trade_group_id = ? AND status = 'Created'",
            (new_status, trade_group_id),
        )


async def _send_trade_group_emergency_alert(
    notifier: TelegramNotifier, trade_group_id: str, exception: Exception
) -> None:
    """Dispatches a critical error notification to Telegram when an unhandled exception occurs."""
    try:
        emergency_message = build_tree_message(
            title="KRITISCHER SYSTEMFEHLER BEI ORDER-VERARBEITUNG",
            context=trade_group_id,
            emoji="🚨",
            rows=[
                ("Fehler", f"<i>{exception}</i>"),
                (
                    "Hinweis",
                    "Verarbeitung unterbrochen. Bitte System manuell prüfen!",
                ),
            ],
        )
        await notifier.send_message(emergency_message)
    except Exception as telegram_error:
        logger.critical(
            "Failed to send emergency Telegram message",
            trade_group_id=trade_group_id,
            error=str(telegram_error),
        )


async def _ensure_entry_order_submitted(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    trade_group_id: str,
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> OrderRow | None:
    """Verifies or submits the ENTRY order, aborting remaining group orders if transmission fails."""
    if entry_order.status == "Created":
        logger.info(
            "Normal entry: Processing ENTRY order",
            trade_group_id=trade_group_id,
        )
        updated_entry = await _process_entry_order(
            db,
            interactive_brokers,
            entry_order,
            child_orders,
            notifier,
            config,
            placed_orders,
        )
        if not updated_entry or updated_entry.status in ("Error", "Cancelled"):
            logger.warning(
                "ENTRY order failed or cancelled. Skipping child orders.",
                trade_group_id=trade_group_id,
            )
            failure_status = (
                "Cancelled"
                if (updated_entry and updated_entry.status == "Cancelled")
                else "Error"
            )
            await _abort_remaining_trade_group_orders(
                db, trade_group_id, failure_status
            )
            return None
        return updated_entry

    if entry_order.status in ("Error", "Cancelled"):
        logger.warning(
            "ENTRY order in terminal failure status. Skipping child orders.",
            trade_group_id=trade_group_id,
        )
        await _abort_remaining_trade_group_orders(
            db, trade_group_id, entry_order.status
        )
        return None

    return entry_order


async def process_trade_group(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    trade_group_id: str,
    notifier: TelegramNotifier,
    config: Config,
) -> None:
    """
    Verarbeitet eine einzelne Trade-Gruppe aus der Queue.

    Übermittelt die ENTRY-Order sowie die zugehörigen Child-Orders (SL, TP, EXIT)
    an die TWS.
    """
    try:
        logger.info("Processing trade group from queue", trade_group_id=trade_group_id)

        orders = await _load_trade_group_orders(db, trade_group_id)
        if not orders:
            logger.warning(
                "No orders found for trade group in DB",
                trade_group_id=trade_group_id,
            )
            return

        entry_order, child_orders = _split_trade_group_orders(orders)
        if not entry_order:
            logger.error(
                "No ENTRY order present in group", trade_group_id=trade_group_id
            )
            return

        is_post_fill: Final[bool] = entry_order.status == "Filled"
        placed_orders: list[OrderRow] = []

        validated_entry = await _ensure_entry_order_submitted(
            db=db,
            interactive_brokers=interactive_brokers,
            entry_order=entry_order,
            child_orders=child_orders,
            trade_group_id=trade_group_id,
            notifier=notifier,
            config=config,
            placed_orders=placed_orders,
        )
        if not validated_entry:
            return

        entry_order = validated_entry

        await _process_child_orders(
            db,
            interactive_brokers,
            entry_order,
            child_orders,
            is_post_fill,
            notifier,
            config,
            placed_orders,
        )

        if placed_orders:
            order_dicts = _format_submitted_orders_summary(placed_orders)
            await notifier.send_bracket_order_submitted(
                symbol=entry_order.symbol,
                trade_group_id=trade_group_id,
                strategy_name=entry_order.strategy_name or "N/A",
                orders=order_dicts,
            )
    except Exception as unhandled_exception:
        logger.exception(
            "CRITICAL: Unhandled exception during trade group processing",
            trade_group_id=trade_group_id,
            error=str(unhandled_exception),
        )
        await _send_trade_group_emergency_alert(
            notifier, trade_group_id, unhandled_exception
        )
        raise


async def _load_trade_group_orders(
    db: aiosqlite.Connection, trade_group_id: str
) -> list[OrderRow]:
    """Lädt alle Orders einer Trade-Gruppe aus der Datenbank."""
    orders: list[OrderRow] = []
    query = """
        SELECT order_id, perm_id, parent_id, trade_group_id, account_id, bracket_role,
               symbol, sec_type, exchange, action, quantity, order_type, target_price, tif, strategy_name,
               status, retry_count, transmitted_at
        FROM orders
        WHERE trade_group_id = ?
    """
    async with db.execute(query, (trade_group_id,)) as cursor:
        async for row in cursor:
            orders.append(order_row_from_db_row(row))
    return orders


def _get_account_value(
    interactive_brokers: IB, account_id: str, tag: str
) -> Decimal | None:
    """Ermittelt einen bestimmten Kontowert von IBKR."""
    for account_value in interactive_brokers.accountValues():
        if account_value.tag != tag:
            continue
        if account_id and account_value.account != account_id:
            continue
        try:
            return Decimal(str(account_value.value))
        except (ValueError, InvalidOperation) as exception:
            logger.warning(
                "Failed to parse account value as Decimal",
                tag=tag,
                value=account_value.value,
                error=str(exception),
            )
    return None


async def _check_cushion_limit(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    config: Config,
    notifier: TelegramNotifier,
) -> tuple[bool, OrderRow, Decimal]:
    """Prüft das globale Account-Cushion Limit.

    Returns:
        (passed_check, updated_entry_order, cushion_percentage)
    """
    cushion_percentage = Decimal("100.0")
    cushion_value = _get_account_value(
        interactive_brokers, entry_order.account_id, "Cushion"
    )
    if cushion_value is not None:
        cushion_percentage = cushion_value * Decimal("100.0")

    min_cushion = Decimal(str(config.account.min_cushion_pct))
    if cushion_value is not None and cushion_value < min_cushion:
        logger.error(
            f"{TAG_CUSHION_ALERT} Cushion check failed. Order blocked.",
            symbol=entry_order.symbol,
            account=entry_order.account_id,
            cushion=f"{cushion_percentage:.1f}%",
            limit=f"{min_cushion * Decimal('100.0'):.1f}%",
        )
        updated_order = dataclasses.replace(entry_order, status="Error")
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                (entry_order.order_id,),
            )
        await notifier.send_margin_limit_exceeded(
            symbol=entry_order.symbol,
            account_id=entry_order.account_id,
            init_margin_after=Decimal("0.0"),
            limit_value=Decimal("0.0"),
            cushion_percentage=cushion_percentage,
        )
        return False, updated_order, cushion_percentage

    return True, entry_order, cushion_percentage


async def _evaluate_margin_warnings(
    interactive_brokers: IB,
    entry_order: OrderRow,
    init_margin_after: Decimal,
    equity_with_loan: Decimal,
    notifier: TelegramNotifier,
) -> None:
    """Sendet bei Bedarf Warnungen bezüglich Cash-Überdeckung oder hoher Margin-Auslastung (>50%)."""
    total_cash = _get_account_value(
        interactive_brokers, entry_order.account_id, "TotalCashValue"
    ) or Decimal("0.0")

    if entry_order.target_price is not None:
        purchase_value = Decimal(str(entry_order.quantity)) * entry_order.target_price
        if purchase_value > total_cash:
            margin_needed = purchase_value - total_cash
            logger.info(
                "Trade requires margin usage.",
                purchase_value=float(purchase_value),
                available_cash=float(total_cash),
                margin_needed=float(margin_needed),
            )
            await notifier.send_margin_utilization_warning(
                symbol=entry_order.symbol,
                account_id=entry_order.account_id,
                purchase_value=purchase_value,
                total_cash=total_cash,
                margin_needed=margin_needed,
            )

    margin_usage_percentage = Decimal("0.0")
    if equity_with_loan > Decimal("0.0"):
        margin_usage_percentage = (init_margin_after / equity_with_loan) * Decimal(
            "100.0"
        )
    if margin_usage_percentage > Decimal("50.0"):
        logger.warning(
            "High margin usage warning.",
            margin_utilization=f"{float(margin_usage_percentage):.1f}%",
            init_margin=float(init_margin_after),
            net_liquidation=float(equity_with_loan),
        )
        await notifier.send_high_margin_usage_warning(
            symbol=entry_order.symbol,
            account_id=entry_order.account_id,
            usage_percentage=margin_usage_percentage,
            init_margin_after=init_margin_after,
            net_liquidation=equity_with_loan,
        )


def _get_whatif_timeout_s(config: Config) -> float:
    """
    Extrahiert den What-If Timeout-Wert sicher aus der Konfiguration.

    Gibt den konfigurierten Wert zurück, oder 10.0 als Fallback, falls kein gültiger
    numerischer Timeout konfiguriert oder der Wert gemockt ist.
    """
    try:
        timeout_value = getattr(getattr(config, "tws", None), "whatif_timeout_s", 10.0)
        if isinstance(timeout_value, int | float):
            return float(timeout_value)
    except Exception:
        pass
    return 10.0


async def handle_reauthorization_wait(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    contract: Contract,
    simulated_order: Order,
    entry_order: OrderRow,
    config: Config,
    notifier: TelegramNotifier,
    slice_sleep_s: float = 10.0,
) -> bool:
    """
    Pausiert die Order-Ausführung bei einer Reautorisierungs-/Token-Anforderung von IBKR.

    Wartet im Intervall `config.app.reauth_check_interval_s` (Standard: 30 Minuten),
    sendet bei jedem Versuch eine Telegram-Warnmeldung und prüft die Freigabe per What-If.
    Storniert die Orders bei Erreichen des Börsenschlusses für das jeweilige Wertpapier.

    Args:
        db: Offene aiosqlite-Datenbankverbindung.
        interactive_brokers: Aktive TWS/Gateway Client-Instanz.
        contract: Das TWS Contract-Objekt.
        simulated_order: Das simulierte Order-Objekt (mit whatIf=True).
        entry_order: Das OrderRow-Datenmodell der betroffenen Order.
        config: Systemkonfiguration.
        notifier: TelegramNotifier für Benachrichtigungen.
        slice_sleep_s: Prüfintervall für Slice-Checks auf Börsenschluss.

    Returns:
        True, wenn die Reautorisierung erfolgreich bestätigt wurde, sonst False (bei Börsenschluss).
    """
    logger.warning(
        f"{TAG_REAUTH_WAIT} Reauthorization required for order. Entering reauth wait loop.",
        symbol=entry_order.symbol,
        trade_group_id=entry_order.trade_group_id,
        order_id=entry_order.order_id,
    )

    attempt = 1
    reauth_interval_minutes = int(config.app.reauth_check_interval_s // 60)
    await notifier.send_message(
        f"🔑 <b>REAUTORISIERUNG ERFORDERLICH (Versuch #{attempt})</b> | <code>{entry_order.symbol}</code>\n"
        f"├─ <b>Status:</b> Ausführung pausiert\n"
        f"├─ <b>Grund:</b> <i>IBKR verlangt Token-Bestätigung im Client Portal!</i>\n"
        f"└─ <b>Nächste Prüfung:</b> In {reauth_interval_minutes} Minuten (Verfall bei Börsenschluss)."
    )

    reauth_interval = float(config.app.reauth_check_interval_s)

    while True:
        if is_market_closed_for_symbol(entry_order.symbol):
            logger.warning(
                "Market closed for symbol while waiting for reauthorization. Cancelling orders.",
                symbol=entry_order.symbol,
                trade_group_id=entry_order.trade_group_id,
            )
            async with transaction(db):
                await db.execute(
                    "UPDATE orders SET status = 'Cancelled' WHERE trade_group_id = ?",
                    (entry_order.trade_group_id,),
                )
            await notifier.send_message(
                f"🚨 <b>SIGNALE VERFALLEN (Börsenschluss erreicht)</b> | <code>{entry_order.symbol}</code>\n"
                f"├─ <b>Status:</b> Orders storniert (Cancelled)\n"
                f"└─ <b>Grund:</b> Keine Reautorisierung bis zum Handelsschluss erfolgt."
            )
            return False

        elapsed = 0.0
        while elapsed < reauth_interval:
            step_sleep = min(slice_sleep_s, reauth_interval - elapsed)
            await asyncio.sleep(step_sleep)
            elapsed += step_sleep

            if is_market_closed_for_symbol(entry_order.symbol):
                logger.warning(
                    "Market closed during reauth interval slice. Cancelling orders.",
                    symbol=entry_order.symbol,
                    trade_group_id=entry_order.trade_group_id,
                )
                async with transaction(db):
                    await db.execute(
                        "UPDATE orders SET status = 'Cancelled' WHERE trade_group_id = ?",
                        (entry_order.trade_group_id,),
                    )
                await notifier.send_message(
                    f"🚨 <b>SIGNALE VERFALLEN (Börsenschluss erreicht)</b> | <code>{entry_order.symbol}</code>\n"
                    f"├─ <b>Status:</b> Orders storniert (Cancelled)\n"
                    f"└─ <b>Grund:</b> Keine Reautorisierung bis zum Handelsschluss erfolgt."
                )
                return False

        while not interactive_brokers.isConnected():
            logger.warning(
                "TWS disconnected during reauth wait. Waiting for reconnection...",
                symbol=entry_order.symbol,
            )
            await asyncio.sleep(5.0)

        attempt += 1
        logger.info(
            "Retrying What-If check for reauthorization",
            symbol=entry_order.symbol,
            attempt=attempt,
        )

        timeout_s = _get_whatif_timeout_s(config)
        try:
            await asyncio.wait_for(
                interactive_brokers.whatIfOrderAsync(contract, simulated_order),
                timeout=timeout_s,
            )
            logger.info(
                "Reauthorization successfully verified via What-If!",
                symbol=entry_order.symbol,
                attempt=attempt,
            )
            await notifier.send_message(
                f"✅ <b>REAUTORISIERUNG ERFOLGREICH</b> | <code>{entry_order.symbol}</code>\n"
                f"├─ <b>Status:</b> Sitzung autorisiert nach {attempt} Versuchen\n"
                f"└─ <b>Aktion:</b> Setze Übertragung der Orders fort..."
            )
            return True

        except Exception as retry_exception:
            error_text = str(retry_exception)
            if is_reauthorization_error(0, error_text):
                logger.warning(
                    "Reauthorization still required on retry",
                    symbol=entry_order.symbol,
                    attempt=attempt,
                    error=error_text,
                )
                await notifier.send_message(
                    f"🔑 <b>REAUTORISIERUNG ERFORDERLICH (Versuch #{attempt})</b> | <code>{entry_order.symbol}</code>\n"
                    f"├─ <b>Status:</b> Ausführung weiterhin pausiert\n"
                    f"├─ <b>Grund:</b> <i>IBKR verlangt weiterhin Token-Bestätigung im Client Portal.</i>\n"
                    f"└─ <b>Nächste Prüfung:</b> In {reauth_interval_minutes} Minuten."
                )
            else:
                logger.warning(
                    "Unexpected error during reauthorization retry What-If",
                    symbol=entry_order.symbol,
                    attempt=attempt,
                    error=error_text,
                )
                await notifier.send_message(
                    f"🔑 <b>REAUTORISIERUNG ERFORDERLICH (Versuch #{attempt})</b> | <code>{entry_order.symbol}</code>\n"
                    f"├─ <b>Status:</b> Prüfung fehlgeschlagen ({error_text})\n"
                    f"└─ <b>Nächste Prüfung:</b> In {reauth_interval_minutes} Minuten."
                )


def _clean_tws_error_message(raw_message: str) -> str:
    """Strips HTML line breaks and normalizes consecutive whitespace in TWS error strings."""
    return re.sub(r"[ \t]+", " ", re.sub(r"(?i)<br\s*/?>", " ", raw_message)).strip()


def _resolve_captured_tws_error(
    captured_errors: Sequence[tuple[int, int, str]],
    fallback_error: Exception,
) -> tuple[int, str]:
    """
    Extracts the most actionable error code and description from captured errorEvent notifications.

    Args:
        captured_errors: Sequence of (request_id, error_code, error_message) captured during simulation.
        fallback_error: The underlying Python exception raised during the simulation await.

    Returns:
        tuple of (error_code, error_message).
    """
    if captured_errors:
        for _request_id, code, message in reversed(captured_errors):
            if (
                is_read_only_error(code, message)
                or is_reauthorization_error(code, message)
                or code != 0
            ):
                return code, message
    return 0, str(fallback_error)


async def _run_whatif_with_error_capture(
    interactive_brokers: IB,
    contract: Contract,
    simulated_order: Order,
    timeout_seconds: float,
    captured_errors: list[tuple[int, int, str]],
) -> Any:
    """
    Executes a What-If simulation with real-time errorEvent listener capture.

    Returns the OrderState returned by TWS if successful within timeout_seconds.
    Raises RuntimeError on captured errorEvent, TimeoutError if timed out, or underlying IB exceptions.
    """
    error_event = asyncio.Event()

    def _on_whatif_error(
        request_id: int, error_code: int, error_string: str, _contract: Any = None
    ) -> None:
        captured_errors.append((request_id, error_code, error_string))
        if is_read_only_error(error_code, error_string) or is_reauthorization_error(
            error_code, error_string
        ):
            error_event.set()

    has_error_event = hasattr(interactive_brokers, "errorEvent") and hasattr(
        interactive_brokers.errorEvent, "connect"
    )
    if has_error_event:
        try:
            interactive_brokers.errorEvent.connect(_on_whatif_error)
        except Exception:
            has_error_event = False

    try:
        whatif_raw = interactive_brokers.whatIfOrderAsync(contract, simulated_order)
        whatif_task = asyncio.ensure_future(whatif_raw)
        error_wait_task = asyncio.ensure_future(error_event.wait())

        done, pending = await asyncio.wait(
            [whatif_task, error_wait_task],
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if error_event.is_set():
            last_error = (
                captured_errors[-1] if captured_errors else (0, 0, "Unknown error")
            )
            raise RuntimeError(f"TWS Error {last_error[1]}: {last_error[2]}")

        if whatif_task in done:
            return whatif_task.result()

        raise TimeoutError()
    finally:
        if has_error_event:
            try:
                interactive_brokers.errorEvent.disconnect(_on_whatif_error)
            except Exception:
                pass


async def _handle_whatif_simulation_failure(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    contract: Contract,
    simulated_order: Order,
    entry_order: OrderRow,
    config: Config,
    notifier: TelegramNotifier,
    exception: Exception,
    captured_errors: list[tuple[int, int, str]],
    whatif_timeout_s: float,
) -> tuple[bool, OrderRow, Any | None]:
    """
    Handles What-If simulation failures (reauthorization, read-only mode, or timeout/fatal error).

    Returns:
        tuple of (simulation_succeeded_or_recovered, updated_entry_order, recovered_order_state)
    """
    specific_error_code, specific_error_msg = _resolve_captured_tws_error(
        captured_errors, exception
    )

    if is_reauthorization_error(specific_error_code, specific_error_msg):
        authorized = await handle_reauthorization_wait(
            db=db,
            interactive_brokers=interactive_brokers,
            contract=contract,
            simulated_order=simulated_order,
            entry_order=entry_order,
            config=config,
            notifier=notifier,
        )
        if authorized:
            try:
                recovered_order_state = await asyncio.wait_for(
                    interactive_brokers.whatIfOrderAsync(contract, simulated_order),
                    timeout=whatif_timeout_s,
                )
                return True, entry_order, recovered_order_state
            except Exception as simulation_exception:
                logger.error(
                    "What-If simulation failed after reauthorization.",
                    symbol=entry_order.symbol,
                    error=str(simulation_exception),
                )
                entry_order = dataclasses.replace(entry_order, status="Error")
                async with transaction(db):
                    await db.execute(
                        "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                        (entry_order.order_id,),
                    )
                return False, entry_order, None
        else:
            entry_order = dataclasses.replace(entry_order, status="Cancelled")
            return False, entry_order, None

    if is_read_only_error(specific_error_code, specific_error_msg):
        clean_error_msg = _clean_tws_error_message(specific_error_msg)
        formatted_reason = f"API im READ-ONLY Modus. Details: {clean_error_msg}"
        logger.error(
            "What-If simulation failed: API in Read-Only mode",
            symbol=entry_order.symbol,
            error=clean_error_msg,
            code=specific_error_code or 321,
        )
        entry_order = dataclasses.replace(entry_order, status="Error")
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                (entry_order.order_id,),
            )
        await notifier.send_order_failed(
            order_id=entry_order.order_id,
            tws_code=specific_error_code or 321,
            reason=formatted_reason,
            symbol=entry_order.symbol,
            bracket_role=entry_order.bracket_role,
            is_fatal=True,
        )
        return False, entry_order, None

    # Generic fail-closed failure or timeout
    logger.error(
        "What-If simulation timed out or failed. Aborting order execution to fail closed.",
        symbol=entry_order.symbol,
        error=str(exception),
    )
    entry_order = dataclasses.replace(entry_order, status="Error")
    async with transaction(db):
        await db.execute(
            "UPDATE orders SET status = 'Error' WHERE order_id = ?",
            (entry_order.order_id,),
        )
    if (
        specific_error_code != 0
        and specific_error_msg
        and specific_error_msg != "Unknown error"
    ):
        clean_error_msg = _clean_tws_error_message(specific_error_msg)
        fail_reason = f"Risk validation simulation failed/timed out (Fail-Closed). Details: {clean_error_msg}"
        fail_code = specific_error_code
    else:
        fail_reason = "Risk validation simulation failed/timed out (Fail-Closed)."
        fail_code = 0

    await notifier.send_order_failed(
        order_id=entry_order.order_id,
        tws_code=fail_code,
        reason=fail_reason,
        symbol=entry_order.symbol,
        bracket_role=entry_order.bracket_role,
        is_fatal=True,
    )
    return False, entry_order, None


async def _check_margin_limit_and_alert(
    db: aiosqlite.Connection,
    entry_order: OrderRow,
    config: Config,
    notifier: TelegramNotifier,
    init_margin_after: Decimal,
    equity_with_loan: Decimal,
    cushion_percentage: Decimal,
) -> tuple[bool, OrderRow]:
    """Prüft, ob die Margin-Anforderung nach der Order das konfigurierte Limit überschreitet."""
    limit_value = equity_with_loan * Decimal(str(config.account.max_margin_usage_pct))
    if init_margin_after > limit_value:
        logger.error(
            "Order blocked due to margin limit violation.",
            required_margin=float(init_margin_after),
            limit=float(limit_value),
            equity=float(equity_with_loan),
        )
        entry_order = dataclasses.replace(entry_order, status="Error")
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET status = 'Error' WHERE order_id = ?",
                (entry_order.order_id,),
            )
        await notifier.send_margin_limit_exceeded(
            symbol=entry_order.symbol,
            account_id=entry_order.account_id,
            init_margin_after=init_margin_after,
            limit_value=limit_value,
            cushion_percentage=cushion_percentage,
        )
        return False, entry_order
    return True, entry_order


async def _verify_margin_and_cushion(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    config: Config,
    notifier: TelegramNotifier,
) -> tuple[bool, OrderRow]:
    """Führt die What-If Simulation und Cushion-Checks aus."""
    passed_cushion, entry_order, cushion_percentage = await _check_cushion_limit(
        db, interactive_brokers, entry_order, config, notifier
    )
    if not passed_cushion:
        return False, entry_order

    contract = make_contract_for_order(entry_order)
    simulated_order = build_order(entry_order)
    whatif_timeout_s = _get_whatif_timeout_s(config)
    captured_errors: list[tuple[int, int, str]] = []

    try:
        order_state = await _run_whatif_with_error_capture(
            interactive_brokers,
            contract,
            simulated_order,
            whatif_timeout_s,
            captured_errors,
        )
    except Exception as simulation_exception:
        success, entry_order, order_state = await _handle_whatif_simulation_failure(
            db=db,
            interactive_brokers=interactive_brokers,
            contract=contract,
            simulated_order=simulated_order,
            entry_order=entry_order,
            config=config,
            notifier=notifier,
            exception=simulation_exception,
            captured_errors=captured_errors,
            whatif_timeout_s=whatif_timeout_s,
        )
        if not success:
            return False, entry_order

    if order_state:
        init_margin_after = Decimal(str(order_state.initMarginAfter or "0.0"))
        equity_with_loan = Decimal(str(order_state.equityWithLoanAfter or "0.0"))
        passed_limit, entry_order = await _check_margin_limit_and_alert(
            db=db,
            entry_order=entry_order,
            config=config,
            notifier=notifier,
            init_margin_after=init_margin_after,
            equity_with_loan=equity_with_loan,
            cushion_percentage=cushion_percentage,
        )
        if not passed_limit:
            return False, entry_order

        await _evaluate_margin_warnings(
            interactive_brokers,
            entry_order,
            init_margin_after,
            equity_with_loan,
            notifier,
        )

    return True, entry_order


async def _transmit_entry_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> OrderRow | None:
    """Weist dem Entry eine TWS Order-ID zu, aktualisiert die DB und übermittelt an TWS."""
    async with ORDER_ID_LOCK:
        tws_order_id = await _get_next_non_colliding_order_id(db, interactive_brokers)
        await _assign_order_id_in_db(db, entry_order.order_id, tws_order_id)

    entry_order = dataclasses.replace(
        entry_order,
        order_id=tws_order_id,
        status="Submitted",
    )

    contract = make_contract_for_order(entry_order)
    ib_entry_order = build_order(entry_order)

    # Den tatsächlich an TWS übermittelten (tick-gerundeten) Preis in OrderRow und DB synchronisieren
    transmitted_price = extract_transmitted_price(ib_entry_order)
    if transmitted_price is not None and transmitted_price != entry_order.target_price:
        logger.debug(
            "Entry price tick-rounded",
            original=entry_order.target_price,
            transmitted=transmitted_price,
            symbol=entry_order.symbol,
        )
        entry_order = dataclasses.replace(entry_order, target_price=transmitted_price)
        await _sync_transmitted_price_to_db(db, tws_order_id, transmitted_price)

    has_unsent_children = any(child.status == "Created" for child in child_orders)
    ib_entry_order.transmit = not has_unsent_children

    logger.info(
        "Sending ENTRY order to TWS", order_id=tws_order_id, symbol=entry_order.symbol
    )
    success = await _place_and_verify_order(
        db,
        interactive_brokers,
        contract,
        ib_entry_order,
        entry_order,
        tws_order_id,
        notifier,
        config=config,
    )
    if not success:
        return dataclasses.replace(entry_order, status="Error")

    placed_orders.append(entry_order)
    await asyncio.sleep(config.app.order_rate_limit_s)
    return entry_order


async def _process_entry_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> OrderRow | None:
    """Valideiert Cushion und Margin, weist dem Entry eine TWS Order-ID zu und übermittelt an TWS."""
    success, entry_order = await _verify_margin_and_cushion(
        db, interactive_brokers, entry_order, config, notifier
    )
    if not success:
        return entry_order

    return await _transmit_entry_order(
        db,
        interactive_brokers,
        entry_order,
        child_orders,
        notifier,
        config,
        placed_orders,
    )


async def _assign_order_id_in_db(
    db: aiosqlite.Connection, original_order_id: int, tws_order_id: int
) -> None:
    """Updates order status and order_id in database."""
    async with transaction(db):
        await db.execute(
            "UPDATE orders SET order_id = ?, status = 'Submitted', transmitted_at = datetime('now') WHERE order_id = ?",
            (tws_order_id, original_order_id),
        )


async def _sync_transmitted_price_to_db(
    db: aiosqlite.Connection, order_id: int, transmitted_price: Decimal
) -> None:
    """Persists the tick-rounded price to the DB so it matches the TWS submission."""
    async with transaction(db):
        await db.execute(
            "UPDATE orders SET target_price = ? WHERE order_id = ?",
            (str(transmitted_price), order_id),
        )


async def _sync_transmitted_tif_to_db(
    db: aiosqlite.Connection, order_id: int, tif: str
) -> None:
    """Persists the updated TIF (e.g. GTD) to the DB so it matches the TWS submission."""
    async with transaction(db):
        await db.execute(
            "UPDATE orders SET tif = ? WHERE order_id = ?",
            (tif, order_id),
        )


async def _process_child_orders(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    entry_order: OrderRow,
    child_orders: list[OrderRow],
    is_post_fill: bool,
    notifier: TelegramNotifier,
    config: Config,
    placed_orders: list[OrderRow],
) -> None:
    """Verarbeitet die verbleibenden untergeordneten Orders (SL, TP, EXIT)."""
    created_children = [child for child in child_orders if child.status == "Created"]
    for iteration_index, child in enumerate(created_children):
        is_last = iteration_index == len(created_children) - 1
        success, updated_child = await _place_single_child_order(
            db,
            interactive_brokers,
            child,
            entry_order,
            is_post_fill,
            is_last,
            notifier,
            config,
            sibling_orders=child_orders,
        )
        if success:
            placed_orders.append(updated_child)


async def _apply_loc_gtd_guard(
    db: aiosqlite.Connection,
    child: OrderRow,
    sibling_orders: Sequence[OrderRow],
    tws_order_id: int,
    ib_child_order: Order,
    notifier: TelegramNotifier,
) -> tuple[bool, OrderRow]:
    """
    Checks and applies GTD cutoff expiration for LMT child orders when an LOC/MOC sibling exists.

    Returns:
        tuple of (can_proceed, updated_child_order)
    """
    if not should_apply_loc_gtd(child, sibling_orders):
        return True, child

    if is_past_loc_gtd_cutoff(child.symbol):
        logger.warning(
            "Skipping LMT exit transmission: LOC GTD cutoff already passed",
            order_id=tws_order_id,
            trade_group_id=child.trade_group_id,
            symbol=child.symbol,
        )
        child = dataclasses.replace(child, status="Cancelled")
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                (tws_order_id,),
            )
        await notifier.send_message(
            f"⚠️ <b>LMT-EXIT ÜBERSPRINGEN (Cut-Off erreicht)</b> | <code>{child.symbol}</code>\n"
            f"├─ <b>Order-ID:</b> <code>{tws_order_id}</code>\n"
            f"├─ <b>Status:</b> Cancelled (nicht übermittelt)\n"
            f"└─ <b>Grund:</b> 15:48 Verfall für LMT erreicht. LOC-Schwester sichert Glattstellung."
        )
        return False, child

    cutoff_string = compute_loc_gtd_cutoff(child.symbol)
    ib_child_order.tif = "GTD"
    ib_child_order.goodTillDate = cutoff_string
    child = dataclasses.replace(child, tif="GTD")
    await _sync_transmitted_tif_to_db(db, tws_order_id, "GTD")
    logger.info(
        "Configured GTD expiry for LMT child order due to LOC/MOC sibling",
        order_id=tws_order_id,
        trade_group_id=child.trade_group_id,
        symbol=child.symbol,
        gtd=cutoff_string,
    )
    return True, child


def _configure_child_order_transmission(
    ib_child_order: Order,
    entry_order_id: int,
    is_post_fill: bool,
    is_last: bool,
) -> None:
    """Configures parentId linkage and transmit flag for child orders."""
    if not is_post_fill:
        ib_child_order.parentId = entry_order_id
    ib_child_order.transmit = True if is_post_fill else is_last


async def _place_single_child_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    child: OrderRow,
    entry_order: OrderRow,
    is_post_fill: bool,
    is_last: bool,
    notifier: TelegramNotifier,
    config: Config,
    sibling_orders: Sequence[OrderRow] = (),
) -> tuple[bool, OrderRow]:
    """Bereitet eine einzelne untergeordnete Order vor und übermittelt sie an TWS."""
    logger.info(
        "Processing child order",
        bracket_role=child.bracket_role,
        trade_group_id=child.trade_group_id,
    )

    if is_post_fill and child.bracket_role in ("SL", "TP", "EXIT"):
        should_continue, child = await _adjust_exit_order_quantity(
            db, interactive_brokers, child, notifier
        )
        if not should_continue:
            return False, child

    async with ORDER_ID_LOCK:
        tws_order_id = await _get_next_non_colliding_order_id(db, interactive_brokers)
        await _assign_order_id_in_db(db, child.order_id, tws_order_id)

    child = dataclasses.replace(
        child,
        order_id=tws_order_id,
        status="Submitted",
    )

    contract = make_contract_for_order(child)
    ib_child_order = build_order(child)

    # Den tatsächlich an TWS übermittelten (tick-gerundeten) Preis in OrderRow und DB synchronisieren
    transmitted_price = extract_transmitted_price(ib_child_order)
    if transmitted_price is not None and transmitted_price != child.target_price:
        logger.debug(
            "Child price tick-rounded",
            original=child.target_price,
            transmitted=transmitted_price,
            role=child.bracket_role,
        )
        child = dataclasses.replace(child, target_price=transmitted_price)
        await _sync_transmitted_price_to_db(db, tws_order_id, transmitted_price)

    can_proceed, child = await _apply_loc_gtd_guard(
        db, child, sibling_orders, tws_order_id, ib_child_order, notifier
    )
    if not can_proceed:
        return False, child

    _configure_child_order_transmission(
        ib_child_order, entry_order.order_id, is_post_fill, is_last
    )

    logger.info(
        "Sending child order to TWS",
        order_id=tws_order_id,
        role=child.bracket_role,
    )
    success = await _place_and_verify_order(
        db,
        interactive_brokers,
        contract,
        ib_child_order,
        child,
        tws_order_id,
        notifier,
        config=config,
    )
    if not success:
        child = dataclasses.replace(child, status="Error")

    await asyncio.sleep(config.app.order_rate_limit_s)
    return success, child


def _is_inactive_child_waiting_for_parent(trade: Trade, ib_order: Order) -> bool:
    """
    Checks whether an Inactive order status is merely waiting for a parent order fill in a bracket.

    Returns:
        True if the order is an Inactive child order with a parentId and no genuine errors, False otherwise.
    """
    if trade.orderStatus.status != "Inactive":
        return False

    parent_id_val = getattr(ib_order, "parentId", 0)
    has_parent = isinstance(parent_id_val, int) and parent_id_val > 0
    if not has_parent:
        return False

    trade_log = getattr(trade, "log", [])
    has_actual_error = any(
        (getattr(entry, "errorCode", 0) not in (0, 399, 2109))
        or (
            getattr(entry, "status", "") in ("ValidationError", "Error")
            and not is_trade_pre_market_held(trade)
        )
        for entry in trade_log
    )
    return not has_actual_error


async def _place_and_verify_order(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    contract: Contract,
    ib_order: Order,
    order_row: OrderRow,
    tws_order_id: int,
    notifier: TelegramNotifier,
    config: Config | None = None,
) -> bool:
    """Sendet die Order und prüft auf Fehler (z. B. Read-Only Modus)."""
    trade = interactive_brokers.placeOrder(contract, ib_order)

    await _wait_for_order_submission(trade)

    if is_trade_pre_market_held(trade):
        logger.info(
            "Order accepted with pre-market hold (warning 399 / held until open)",
            order_id=tws_order_id,
            symbol=order_row.symbol,
            status=trade.orderStatus.status,
        )
        return True

    if trade.orderStatus.status in (
        "Inactive",
        "Cancelled",
        "ValidationError",
        "Error",
    ) and not _is_inactive_child_waiting_for_parent(trade, ib_order):
        return await _handle_order_rejection(
            db,
            trade,
            order_row,
            tws_order_id,
            notifier,
            interactive_brokers=interactive_brokers,
            config=config,
        )

    return True


async def _wait_for_order_submission(trade: Trade) -> None:
    """Kurz warten, um sofortige Ablehnungen (z.B. ValidationError) zu erkennen."""
    for _ in range(20):
        if trade.orderStatus.status not in ("PendingSubmit", "PendingCancel"):
            break
        await asyncio.sleep(0.1)


def _extract_error_from_trade_log(trade_log: Sequence[Any]) -> tuple[str, int]:
    """
    Extracts the first actionable (non-pre-market) error message and error code from a trade log.

    Returns:
        tuple of (error_message, error_code). If no error is found, returns ("Unknown error", 0).
    """
    for entry in trade_log:
        code = getattr(entry, "errorCode", 0)
        status = getattr(entry, "status", "")
        message = getattr(entry, "message", "")
        if (
            (code != 0 or status in ("ValidationError", "Error"))
            and code not in (399, 2109)
            and not is_pre_market_hold_notice(error_code=code, message=message)
        ):
            return str(message), int(code)
    return "Unknown error", 0


def _is_only_benign_trade_warnings(
    trade: Trade, current_trade_log: Sequence[Any]
) -> bool:
    """Checks whether the trade log contains only harmless warnings (399/2109 pre-market hold)."""
    log_errors = [
        entry
        for entry in current_trade_log
        if getattr(entry, "errorCode", 0) != 0
        or getattr(entry, "status", "") in ("ValidationError", "Error")
    ]
    if not log_errors:
        return False

    return all(
        getattr(entry, "errorCode", 0) in (399, 2109)
        or is_pre_market_hold_notice(
            error_code=getattr(entry, "errorCode", 0),
            message=getattr(entry, "message", ""),
        )
        or (
            getattr(entry, "status", "") in ("ValidationError", "Error")
            and getattr(entry, "errorCode", 0) == 0
            and is_trade_pre_market_held(trade, current_trade_log)
        )
        for entry in log_errors
    )


def _extract_fallback_trade_error(trade: Trade) -> tuple[str, int]:
    """Extracts error message from reversed trade log entries or whyHeld status when initial poll has no message."""
    trade_log = getattr(trade, "log", [])
    if trade_log:
        for entry in reversed(trade_log):
            message = getattr(entry, "message", "")
            if message and message.strip():
                return message.strip(), int(getattr(entry, "errorCode", 0))

    why_held = getattr(trade.orderStatus, "whyHeld", None)
    if why_held:
        return str(why_held), 0

    status = getattr(trade.orderStatus, "status", "Unknown")
    return f"Order im Status '{status}' abgelehnt oder inaktiviert.", 0


def _classify_rejection_reason(
    tws_code: int, clean_error_message: str
) -> tuple[str, int, bool]:
    """
    Classifies an order rejection message into formatted reason text, TWS code, and fatal flag.

    Returns:
        tuple of (formatted_reason, resolved_tws_code, is_fatal)
    """
    reason_upper = clean_error_message.upper()
    if is_read_only_error(tws_code, clean_error_message):
        return (
            f"API im READ-ONLY Modus. Details: {clean_error_message}",
            (321 if tws_code == 0 else tws_code),
            True,
        )

    if (
        "LOGIN TO CLIENT PORTAL" in reason_upper
        or "VERIFY USING THE TOKEN" in reason_upper
        or "VERIFICATION PROCESS" in reason_upper
        or ("TOKEN" in reason_upper and "VERIFY" in reason_upper)
    ):
        formatted = (
            "🔑 ANMELDUNG/VERIFIZIERUNG ERFORDERLICH: IBKR/CapTrader verlangt "
            f"Token-Bestätigung im Client Portal! Details: {clean_error_message}"
        )
        return formatted, (201 if tws_code == 0 else tws_code), True

    return clean_error_message, tws_code, False


async def _poll_for_rejection_error(
    trade: Trade,
    order_row: OrderRow,
    tws_order_id: int,
) -> tuple[str, int] | None:
    """
    Polls up to 1 second for asynchronous TWS error events.

    Returns:
        tuple of (error_message, error_code), or None if the order is actually active or pre-market held.
    """
    current_trade_log: list[Any] = []
    error_msg = "Unknown error"
    tws_code = 0

    for _ in range(10):
        if trade.orderStatus.status in ("Submitted", "PreSubmitted"):
            return None

        current_trade_log = getattr(trade, "log", [])
        if is_trade_pre_market_held(trade, current_trade_log):
            logger.info(
                "Ignoring pre-market hold warning (399/2109) during order placement",
                order_id=tws_order_id,
                symbol=order_row.symbol,
            )
            return None

        error_msg, tws_code = _extract_error_from_trade_log(current_trade_log)
        if error_msg != "Unknown error":
            break

        await asyncio.sleep(0.1)

    if is_trade_pre_market_held(trade, current_trade_log):
        logger.info(
            "Ignoring pre-market hold warning (399/2109) during order placement",
            order_id=tws_order_id,
            symbol=order_row.symbol,
        )
        return None

    if (
        _is_only_benign_trade_warnings(trade, current_trade_log)
        and error_msg == "Unknown error"
    ):
        logger.info(
            "Ignoring benign warning (399/2109) during order placement (no real error received)",
            order_id=tws_order_id,
            symbol=order_row.symbol,
        )
        return None

    if error_msg == "Unknown error":
        error_msg, tws_code = _extract_fallback_trade_error(trade)

    return error_msg, tws_code


async def _retry_order_after_reauthorization(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    order_row: OrderRow,
    tws_order_id: int,
    config: Config,
    notifier: TelegramNotifier,
) -> bool:
    """Attempts reauthorization wait loop and re-places order if successful."""
    contract = make_contract_for_order(order_row)
    simulated_order = build_order(order_row)
    simulated_order.whatIf = True
    authorized = await handle_reauthorization_wait(
        db=db,
        interactive_brokers=interactive_brokers,
        contract=contract,
        simulated_order=simulated_order,
        entry_order=order_row,
        config=config,
        notifier=notifier,
    )
    if authorized:
        logger.info(
            "Re-transmitting order after successful reauthorization",
            order_id=tws_order_id,
            symbol=order_row.symbol,
        )
        retry_order = build_order(order_row)
        retry_trade = interactive_brokers.placeOrder(contract, retry_order)
        await _wait_for_order_submission(retry_trade)
        return retry_trade.orderStatus.status not in (
            "Inactive",
            "Cancelled",
            "ValidationError",
            "Error",
        )
    return False


async def _handle_order_rejection(
    db: aiosqlite.Connection,
    trade: Trade,
    order_row: OrderRow,
    tws_order_id: int,
    notifier: TelegramNotifier,
    interactive_brokers: IB | None = None,
    config: Config | None = None,
) -> bool:
    """Behandelt Fehlermeldungen bei der Order-Übertragung."""
    rejection = await _poll_for_rejection_error(trade, order_row, tws_order_id)
    if rejection is None:
        return True

    error_msg, tws_code = rejection
    logger.error(
        f"{TAG_ORDER_REJECT} Order transmission failed",
        order_id=tws_order_id,
        status=trade.orderStatus.status,
        symbol=order_row.symbol,
        error=error_msg,
    )

    clean_error_msg = _clean_tws_error_message(error_msg)

    if (
        interactive_brokers is not None
        and config is not None
        and is_reauthorization_error(tws_code, clean_error_msg)
    ):
        return await _retry_order_after_reauthorization(
            db=db,
            interactive_brokers=interactive_brokers,
            order_row=order_row,
            tws_order_id=tws_order_id,
            config=config,
            notifier=notifier,
        )

    async with transaction(db):
        await db.execute(
            "UPDATE orders SET status = 'Error' WHERE order_id = ?",
            (tws_order_id,),
        )

    formatted_reason, code, is_fatal = _classify_rejection_reason(
        tws_code, clean_error_msg
    )

    await notifier.send_order_failed(
        order_id=tws_order_id,
        tws_code=code,
        reason=formatted_reason,
        symbol=order_row.symbol,
        bracket_role=order_row.bracket_role,
        is_fatal=is_fatal,
    )
    return False


async def _adjust_exit_order_quantity(
    db: aiosqlite.Connection,
    interactive_brokers: IB,
    child: OrderRow,
    notifier: TelegramNotifier,
) -> tuple[bool, OrderRow]:
    """Gleicht Depotbestand ab und passt die Order-Menge an oder storniert sie."""
    live_position = _get_live_position_quantity(
        interactive_brokers, child.account_id, child.symbol
    )

    available_quantity = (
        max(Decimal("0.0"), live_position)
        if child.action == "SELL"
        else max(Decimal("0.0"), -live_position)
    )

    if available_quantity <= Decimal("0.0"):
        await _cancel_empty_exit_order(db, child, live_position, notifier)
        return False, dataclasses.replace(child, status="Cancelled")

    intended_quantity = Decimal(str(child.quantity))
    if available_quantity < intended_quantity:
        child = dataclasses.replace(child, quantity=int(available_quantity))
        await _reduce_exit_order_quantity(
            db, child, intended_quantity, available_quantity, notifier
        )

    return True, child


async def _cancel_empty_exit_order(
    db: aiosqlite.Connection,
    child: OrderRow,
    live_position: Decimal,
    notifier: TelegramNotifier,
) -> None:
    """Storniert eine Exit-Order bei fehlender Gegenposition im Depot."""
    logger.warning(
        "No open counter-position found in portfolio. Exit order will be cancelled.",
        trade_group_id=child.trade_group_id,
        symbol=child.symbol,
        bracket_role=child.bracket_role,
        live_position=float(live_position),
    )
    try:
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET status = 'Cancelled' WHERE order_id = ?",
                (child.order_id,),
            )
    except Exception as exception:
        logger.error(
            "Error cancelling child order in DB",
            order_id=child.order_id,
            error=str(exception),
        )

    await notifier.send_importer_info(
        title="EXIT ABGEBROCHEN",
        file_name=child.trade_group_id,
        status="Storniert",
        details=f"Keine offene Position für {child.symbol} vorhanden (Depotbestand: {float(live_position)}).",
        emoji="⚠️",
    )


async def _reduce_exit_order_quantity(
    db: aiosqlite.Connection,
    child: OrderRow,
    intended_quantity: Decimal,
    available_quantity: Decimal,
    notifier: TelegramNotifier,
) -> None:
    """Reduziert Exit-Menge auf verbleibenden Depotbestand."""
    logger.info(
        "Exit order quantity adjusted to actual portfolio position",
        trade_group_id=child.trade_group_id,
        old_qty=float(intended_quantity),
        new_qty=float(available_quantity),
    )

    try:
        async with transaction(db):
            await db.execute(
                "UPDATE orders SET quantity = ? WHERE order_id = ?",
                (child.quantity, child.order_id),
            )
    except Exception as exception:
        logger.error(
            "Error updating child order quantity in DB",
            order_id=child.order_id,
            error=str(exception),
        )

    await notifier.send_importer_info(
        title="EXIT MENGE ANGEPASST",
        file_name=child.trade_group_id,
        status="Reduziert",
        details=f"Stückzahl für {child.symbol} von {float(intended_quantity)} auf {float(available_quantity)} reduziert.",
        emoji="⚠️",
    )


def _get_live_position_quantity(
    interactive_brokers: IB, account_id: str, symbol: str
) -> Decimal:
    """Ermittelt den aktuellen Depotbestand für ein bestimmtes Symbol und Account.

    Unterstützt sowohl Standard-Aktien (symbol) als auch Futures, bei denen
    IBKR das Root-Symbol (z. B. MNQ) und das Kontrakt-Symbol (z. B. MNQU6) trennt.
    """
    target_symbol = normalize_symbol(symbol)
    for position in interactive_brokers.positions():
        if position.account != account_id:
            continue
        raw_symbol = getattr(position.contract, "symbol", "")
        contract_symbol = (
            normalize_symbol(raw_symbol) if isinstance(raw_symbol, str) else ""
        )
        raw_local_symbol = getattr(position.contract, "localSymbol", "")
        contract_local_symbol = (
            normalize_symbol(raw_local_symbol)
            if isinstance(raw_local_symbol, str)
            else ""
        )
        if target_symbol in (contract_symbol, contract_local_symbol):
            return Decimal(str(position.position))
    return Decimal("0.0")


async def _get_next_non_colliding_order_id(
    db: aiosqlite.Connection, interactive_brokers: IB
) -> int:
    """Ermittelt die nächste gültige Order-ID zur Abwehr von DB-ID-Kollisionen."""
    async with db.execute("SELECT MAX(order_id) FROM orders") as cursor:
        row = await cursor.fetchone()
        max_db_id = int(row[0]) if (row and row[0] is not None) else 0

    tws_next_id = int(interactive_brokers.client.getReqId())
    if max_db_id >= tws_next_id:
        return max_db_id + 1
    return tws_next_id
