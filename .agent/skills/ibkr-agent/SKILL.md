---
name: ibkr-agent
description: "Leverage Interactive Brokers (IBKR) API via ib_async. Useful for checking account details, fetching open positions/orders, placing new orders, and handling real-time data."
---

> [!IMPORTANT]
> Must strictly respect `.agent/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# IBKR API Agent Skill

This skill provides comprehensive instructions, examples, and patterns for interacting with the Interactive Brokers (IBKR) API using the `ib_async` library. It details how to connect, query positions/account info/orders, place trades safely, handle errors, and mock the API during tests.

## Installation & Prerequisites

To use this skill, ensure the following dependencies are available in the runtime environment:

- **Library**: `ib_async` >= 1.0.0 (asynchronous wrapper for the official Python IB API)
- **TWS / Gateway**: An active Trader Workstation (TWS) or IB Gateway running and accepting API connections (typically port `7497` for paper trading or as configured in `config.toml`).

## Core Concepts

### 1. The `IB` Client Instance
The central class is `ib_async.IB`. All communication with the TWS/Gateway passes through this client. It runs fully in an asynchronous event loop (`asyncio`).

```python
import asyncio
from ib_async import IB

async def main() -> None:
    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", 7497, clientId=1)
        print("Connected successfully!")
    finally:
        ib.disconnect()
```

### 2. Async Connection Guidelines
- **Port Matching**: Always match the TWS configuration. Default paper trading port is `7497`, live trading is `7496`. In internal environments, custom ports (like `8888`) may be defined.
- **Client ID**: Choose unique client IDs for separate connections. A client ID of `0` is typically reserved for the primary system to bind to manual orders.
- **Socket Keepalive**: Enable socket TCP keepalive to prevent silent connection dropouts.

---

## Routing & Guidelines

### A. Querying Account & Position Metrics
- **Positions**: Retrieve using `ib.positions()`. This returns a list of `Position` objects.
- **Account Values**: Retrieve using `ib.accountValues()`. Filters for specific fields like `NetLiquidation` or `BuyingPower` as needed.
- **Open Orders**: Request asynchronously with `await ib.reqOpenOrdersAsync()`.
- **Completed Orders**: Request asynchronously with `await ib.reqCompletedOrdersAsync(apiOnly=False)`.

```python
# Querying positions
positions = ib.positions()
for pos in positions:
    print(f"Contract: {pos.contract.symbol}, Position Size: {pos.position}, Avg Cost: {pos.averageCost}")
```

### B. Placing & Managing Orders
- **Contract Definition**: Define contracts precisely. Always specify primary exchange (e.g. `primaryExchange="IBIS2"` or `primaryExchange="NASDAQ"`) to avoid ambiguous contract errors.
- **Order Construction**: Construct using `ib_async` Order classes (e.g. `MarketOrder`, `LimitOrder`).
- **OCA Groups**: One-Cancels-All (OCA) orders (like Bracket setups) must use `ocaType = 3` (reduce with no block) for LOC/MOC orders to comply with IBKR rules.
- **Idempotency**: Check for existing orders or positions before submitting to prevent double-executions.

### C. Error Handling & Stability
- **Connection Loss**: Monitor connectivity and implement exponential backoff reconnection.
- **Timeouts**: Wrap network-sensitive calls in `asyncio.wait_for`.
- **API Warnings**: Handle system warnings gracefully. Do not raise exceptions on warnings (like warning code `2104` - market data farm connection is OK).
- **Exceptions**: Catch `ConnectionError`, `asyncio.TimeoutError`, and `OSError` at system boundaries.

---

## Architectural Constraints (from `python.md`)

1. **Strict Typing**: Ensure all functions, variables, and parameters are type-hinted.
2. **Google-Style Docstrings**: Document public APIs, explaining the "Why" and "What" clearly.
3. **No Pydantic**: Use `@dataclass(frozen=True)` or `TypedDict` for data structures.
4. **Functional Core / Imperative Shell**: Keep calculations (like sizing, allocation, risk calculations) pure, and confine TWS API interactions and DB operations to the Imperative Shell.
5. **No Hardcoded Credentials**: Load connection parameters (host, port, clientId) from configuration or environment.
6. **Strict Conciseness**: Strictly adhere to [.agent/rules/concise.md](.agent/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.
