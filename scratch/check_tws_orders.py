import asyncio
import traceback
from pathlib import Path
from ib_async import IB
from app.core.config import load_config

async def main():
    root_dir = Path(__file__).resolve().parent.parent
    config = load_config(root_dir)
    print(f"Connecting to TWS at {config.tws.host}:{config.tws.port}...")
    ib = IB()
    try:
        await ib.connectAsync(
            config.tws.host,
            config.tws.port,
            clientId=99,
            timeout=10.0
        )
        print("Connected successfully!")
        
        print("Requesting all open orders...")
        await ib.reqAllOpenOrdersAsync()
        
        trades = ib.openTrades()
        print(f"Open trades count: {len(trades)}")
        for trade in trades:
            order = trade.order
            status = trade.orderStatus
            print(f"OrderId: {order.orderId}, PermId: {order.permId}, Action: {order.action}, Symbol: {trade.contract.symbol}, Status: {status.status}")
            
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    ib = IB()
    ib.run(main())
