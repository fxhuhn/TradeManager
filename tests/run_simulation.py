import asyncio
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# PYTHONPATH auf aktuellen Ordner setzen
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog
from ib_async import IB, Stock, Order, Trade, Fill, CommissionReport
from app.core.logging_setup import configure_logging
from app.core.config import load_config
from app.core.db import get_db, verify_db_integrity, run_migrations
from app.services.notifier import TelegramNotifier
from app.services.importer import run_csv_import
from app.services.alert_watcher import alert_watcher
from app.trading.callbacks import TwsCallbacksManager
from app.trading.worker import execution_worker, ORDER_ID_LOCK
from app.trading.recovery import run_recovery
from app.trading.settlement import trigger_settlement
from app.trading.retry import handle_retriable_error

logger = structlog.get_logger()

class MockTwsClient:
    def __init__(self):
        self.next_order_id = 100

    def getReqId(self) -> int:
        oid = self.next_order_id
        self.next_order_id += 1
        return oid


class MockAccountValue:
    def __init__(self, tag: str, value: str, account: str):
        self.tag = tag
        self.value = value
        self.account = account


class MockEvent:
    def __init__(self):
        self.callbacks = []

    def connect(self, cb):
        if cb not in self.callbacks:
            self.callbacks.append(cb)

    def disconnect(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)

    def trigger(self, *args, **kwargs):
        for cb in self.callbacks:
            try:
                cb(*args, **kwargs)
            except Exception as e:
                logger.error("Fehler in Event-Callback", error=str(e))


class MockIB:
    def __init__(self):
        self.client = MockTwsClient()
        self.orderStatusEvent = MockEvent()
        self.execDetailsEvent = MockEvent()
        self.commissionReportEvent = MockEvent()
        self.errorEvent = MockEvent()
        self.disconnectedEvent = MockEvent()
        
        self.completedOrderEvent = MockEvent()
        self.openOrderEndEvent = MockEvent()
        
        self.accountSummaryEvent = MockEvent()

        self._isConnected = False
        self._trades = []
        self._open_trades = []

    def isConnected(self) -> bool:
        return self._isConnected

    async def connectAsync(self, host: str, port: int, clientId: int) -> None:
        self._isConnected = True
        logger.info("MOCK TWS: Verbindung asynchron hergestellt", host=host, port=port, clientId=clientId)

    def reqAutoOpenOrders(self, autoBind: bool) -> None:
        logger.info("MOCK TWS: reqAutoOpenOrders aufgerufen", autoBind=autoBind)

    def disconnect(self) -> None:
        self._isConnected = False
        logger.info("MOCK TWS: getrennt")

    def accountValues(self) -> list:
        # 100.000 USD verfügbares Kapital simulieren
        return [
            MockAccountValue("AvailableFunds", "100000.0", "DU12345"),
            MockAccountValue("AvailableFunds", "100000.0", "U19605236")
        ]

    def reqAccountSummary(self, *args) -> None:
        logger.info("MOCK TWS: reqAccountSummary aufgerufen")
        # Sofort Callback auslösen
        self.accountSummaryEvent.trigger(1, "DU12345", "AvailableFunds", "100000.0", "USD")
        self.accountSummaryEvent.trigger(2, "U19605236", "AvailableFunds", "100000.0", "USD")

    def cancelAccountSummary(self, *args) -> None:
        pass

    def openTrades(self) -> list:
        return self._open_trades

    def trades(self) -> list:
        return self._trades

    def reqOpenOrders(self) -> None:
        logger.info("MOCK TWS: reqOpenOrders aufgerufen")
        # openOrderEndEvent auslösen
        asyncio.create_task(self._trigger_open_order_end())

    async def reqOpenOrdersAsync(self) -> None:
        logger.info("MOCK TWS: reqOpenOrdersAsync aufgerufen")
        await asyncio.sleep(0.05)

    async def _trigger_open_order_end(self):
        await asyncio.sleep(0.1)
        self.openOrderEndEvent.trigger()

    def reqCompletedOrders(self, apiOnly: bool) -> None:
        logger.info("MOCK TWS: reqCompletedOrders aufgerufen", apiOnly=apiOnly)
        asyncio.create_task(self._trigger_completed_end())

    async def reqCompletedOrdersAsync(self, apiOnly: bool = False) -> None:
        logger.info("MOCK TWS: reqCompletedOrdersAsync aufgerufen", apiOnly=apiOnly)
        await asyncio.sleep(0.05)

    async def _trigger_completed_end(self):
        await asyncio.sleep(0.1)
        self.completedOrderEvent.trigger()

    def placeOrder(self, contract: Stock, order: Order) -> Trade:
        logger.info(
            "MOCK TWS: placeOrder aufgerufen", 
            orderId=order.orderId, 
            symbol=contract.symbol,
            action=order.action,
            qty=order.totalQuantity,
            type=order.orderType,
            price=getattr(order, 'lmtPrice', getattr(order, 'auxPrice', None)),
            orderRef=getattr(order, 'orderRef', None),
            transmit=order.transmit
        )

        trade = MagicMock(spec=Trade)
        trade.contract = contract
        trade.order = order
        trade.orderStatus = MagicMock()
        trade.orderStatus.status = "Submitted"
        trade.orderStatus.permId = 99000 + order.orderId

        # Trade asynchron in TWS abwickeln (Ausführung simulieren)
        asyncio.create_task(self._simulate_order_fill(contract, order, trade))
        return trade

    async def _simulate_order_fill(self, contract: Stock, order: Order, trade: Trade):
        # 1. Status: Submitted melden
        await asyncio.sleep(0.2)
        trade.orderStatus.status = "Submitted"
        self.orderStatusEvent.trigger(trade)

        # Bei ENTRY oder EXIT/SL/TP: Ausführung simulieren
        # Wenn Entry, setzen wir es nach 0.5s auf Filled
        await asyncio.sleep(0.5)
        trade.orderStatus.status = "Filled"
        self.orderStatusEvent.trigger(trade)

        # 2. Teilausführung (Execution) melden
        fill = MagicMock(spec=Fill)
        fill.contract = contract
        fill.execution = MagicMock()
        fill.execution.execId = f"EXEC_{order.orderId}_1"
        fill.execution.orderId = order.orderId
        fill.execution.price = getattr(order, 'lmtPrice', getattr(order, 'auxPrice', 180.0))
        fill.execution.shares = order.totalQuantity
        fill.execution.time = "2026-05-31 12:00:00"
        
        self.execDetailsEvent.trigger(trade, fill)

        # 3. Gebühren (Commission Report) melden
        await asyncio.sleep(0.1)
        comm = MagicMock(spec=CommissionReport)
        comm.commission = 2.50
        comm.currency = "USD"
        self.commissionReportEvent.trigger(trade, fill, comm)


async def run_detailed_test():
    """Führt eine vollständige End-to-End-Simulation des Systems aus."""
    logger.info("====================================================")
    logger.info("STARTE AUSFÜHRLICHEN MOCK-SYSTEMTEST (SIMULATION)")
    logger.info("====================================================")

    root_dir = Path(__file__).resolve().parent.parent
    db_path = root_dir / "data" / "simulation_trading.db"
    
    # Alte Simulations-DB löschen falls vorhanden für frischen Durchlauf
    if db_path.exists():
        db_path.unlink()

    # 1. DB initialisieren & Migrationen einspielen
    logger.info("1. DB initialisieren und Migrationen einspielen")
    db = await get_db(db_path)
    migrations_dir = root_dir / "migrations"
    await run_migrations(db, migrations_dir)
    await db.close()

    # 2. Mock TWS & System-Komponenten aufbauen
    logger.info("2. Mock-TWS und Komponenten initialisieren")
    ib = MockIB()
    config = load_config(root_dir)
    notifier = TelegramNotifier(config)

    # DB Factory Hilfsfunktion
    async def db_factory():
        return await get_db(db_path)

    # Queue und asynchrone Callbacks
    queue = asyncio.Queue()

    async def trigger_settlement_cb(tg_id: str, acc_id: str):
        await trigger_settlement(db_factory, tg_id, acc_id, notifier)

    async def handle_retriable_error_cb(order_id: int):
        await handle_retriable_error(db_factory, order_id, queue, notifier, config)

    async def run_recovery_cb():
        db_conn = await db_factory()
        try:
            await run_recovery(db_conn, ib, queue, notifier, trigger_settlement_cb, config)
        finally:
            await db_conn.close()

    # Callbacks verbinden
    callbacks_mgr = TwsCallbacksManager(
        db_factory=db_factory,
        ib=ib,
        notifier=notifier,
        config=config,
        trigger_settlement_cb=trigger_settlement_cb,
        handle_retriable_error_cb=handle_retriable_error_cb,
        run_recovery_cb=run_recovery_cb
    )
    callbacks_mgr.register_all()

    # 3. Phase 1: Preflight & Connection
    logger.info("3. Phase 1: Preflight & Verbindung simulieren")
    is_db_ok = await verify_db_integrity(db_path)
    logger.info("DB Integritaet verifiziert", is_ok=is_db_ok)
    
    await ib.connectAsync(config.tws.host, config.tws.port, config.tws.client_id)
    ib.reqAutoOpenOrders(True)

    # 4. Phase 2: Recovery ausführen
    logger.info("4. Phase 2: Recovery ausführen")
    db_conn = await db_factory()
    try:
        await run_recovery(db_conn, ib, queue, notifier, trigger_settlement_cb, config)
    finally:
        await db_conn.close()

    # 5. Phase 3: CSV-Import ausführen (mit angegebener oder Standard-CSV-Datei)
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "data/orders.csv"
    logger.info(f"5. Phase 3: CSV-Import ausführen ({csv_file})")
    csv_path = root_dir / csv_file
    db_conn = await db_factory()
    try:
        await run_csv_import(db_conn, ib, csv_path, queue, notifier, config)
    finally:
        await db_conn.close()

    # 6. Hintergrunddienste (Worker & Watcher) starten
    logger.info("6. Dauerbetrieb-Hintergrunddienste starten")
    worker_task = asyncio.create_task(
        execution_worker(db_factory, ib, queue, notifier, config)
    )
    watcher_task = asyncio.create_task(
        alert_watcher(
            db_factory=db_factory,
            notifier=notifier,
            config=config,
            interval_seconds=2,  # Beschleunigt für die Simulation
            dead_order_threshold_min=1,
            max_slippage_pct=0.01
        )
    )

    # 7. Simulation laufen lassen
    # Wir warten 5 Sekunden, damit der Worker die Queue verarbeitet und
    # die Fills/Settlements asynchron durchgespielt werden.
    logger.info("7. Warte auf asynchrone Order-Abarbeitung, Fills und Settlements...")
    await asyncio.sleep(5.0)

    # 8. Hintergrund-Tasks beenden
    logger.info("8. Hintergrund-Tasks sauber herunterfahren")
    worker_task.cancel()
    watcher_task.cancel()
    try:
        await asyncio.gather(worker_task, watcher_task, return_exceptions=True)
    except Exception:
        pass
    
    ib.disconnect()

    # 9. Testergebnisse aus DB auswerten und validieren
    logger.info("====================================================")
    logger.info("AUSWERTUNG DER DATENBANK-ZUSTÄNDE")
    logger.info("====================================================")

    db_conn = await db_factory()
    
    # 9a. Orders auswerten
    logger.info("--- Tabelle: orders ---")
    async with db_conn.execute("SELECT order_id, trade_group_id, bracket_role, symbol, quantity, target_price, status FROM orders") as cursor:
        async for r in cursor:
            logger.info(
                f"Order ID: {r['order_id']} | Gruppe: {r['trade_group_id']} | "
                f"Rolle: {r['bracket_role']} | Symbol: {r['symbol']} | "
                f"Menge: {r['quantity']} | Target-Preis: {r['target_price']} | "
                f"Status: {r['status']}"
            )

    # 9b. Executions auswerten
    logger.info("--- Tabelle: executions ---")
    async with db_conn.execute("SELECT exec_id, order_id, price, qty, commission, currency FROM executions") as cursor:
        async for r in cursor:
            logger.info(
                f"Exec ID: {r['exec_id']} | Order ID: {r['order_id']} | "
                f"Preis: {r['price']:.2f} | Menge: {r['qty']} | "
                f"Gebühr: {r['commission']} {r['currency']}"
            )

    # 9c. Settlement auswerten
    logger.info("--- Tabelle: trades_settlement ---")
    async with db_conn.execute("SELECT trade_group_id, avg_entry_price, avg_exit_price, price_diff_slippage, total_commissions, net_pnl FROM trades_settlement") as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            logger.info(
                f"Gruppe: {r['trade_group_id']} | Entry VWAP: {r['avg_entry_price']:.2f} | "
                f"Exit VWAP: {r['avg_exit_price']:.2f} | Slippage: {r['price_diff_slippage']:.4f} | "
                f"Gesamt-Gebühren: {r['total_commissions']:.2f} USD | "
                f"Netto-PnL: {r['net_pnl']:.2f} USD"
            )
        # Nur wenn auch mindestens eine Exit-Order vorhanden ist, erwarten wir ein Settlement
        async with db_conn.execute("SELECT count(*) as cnt FROM orders WHERE bracket_role IN ('SL', 'TP', 'EXIT')") as c:
            has_exits = (await c.fetchone())["cnt"] > 0

        if has_exits:
            assert len(rows) > 0, "Es wurde kein Settlement-Eintrag generiert!"
        else:
            logger.info("Keine Exit-Orders vorhanden, daher kein Settlement erwartet.")

    await db_conn.close()
    
    # Aufräumen der Simulations-DB
    if db_path.exists():
        db_path.unlink()

    logger.info("====================================================")
    logger.info("MOCK-SYSTEMTEST ERFOLGREICH BEENDET (100% SUCCESS)")
    logger.info("====================================================")

if __name__ == "__main__":
    asyncio.run(run_detailed_test())
