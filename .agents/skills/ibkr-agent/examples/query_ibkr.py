"""
Example script demonstrating how to connect to TWS/Gateway and query basic data.

This script follows the Functional Core / Imperative Shell architecture:
- The Imperative Shell connects to the TWS, fetches positions and account values.
- The Functional Core performs pure computations on the fetched data (e.g., calculating margin cushion).
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ib_async import IB, Position

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("query_ibkr")

# Try to import project Config; fallback to defaults if run standalone
try:
    from app.core.config import load_config

    PROJECT_CONFIG_AVAILABLE = True
except ImportError:
    PROJECT_CONFIG_AVAILABLE = False

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 7497
DEFAULT_CLIENT_ID: Final[int] = 99


@dataclass(frozen=True)
class AccountMetrics:
    """Immutable data structure representing key account metrics (Functional Core)."""

    net_liquidation: float
    total_margin: float
    available_funds: float
    cushion_ratio: float


# =====================================================================
# FUNCTIONAL CORE (Pure Logic)
# =====================================================================


def calculate_account_metrics(
    net_liquidation: float,
    total_margin: float,
    available_funds: float,
) -> AccountMetrics:
    """
    Pure Function: Calculates metrics based on raw account parameters.

    Args:
        net_liquidation: The total value of the account if liquidated.
        total_margin: The current margin requirement.
        available_funds: The funds available for new trades.

    Returns:
        An immutable AccountMetrics object.
    """
    if net_liquidation <= 0.0:
        return AccountMetrics(
            net_liquidation=net_liquidation,
            total_margin=total_margin,
            available_funds=available_funds,
            cushion_ratio=0.0,
        )

    # Cushion is the percentage of excess equity relative to net liquidation
    cushion_ratio = max(0.0, available_funds / net_liquidation)
    return AccountMetrics(
        net_liquidation=net_liquidation,
        total_margin=total_margin,
        available_funds=available_funds,
        cushion_ratio=cushion_ratio,
    )


# =====================================================================
# IMPERATIVE SHELL (I/O, Networking, Orchestration)
# =====================================================================


async def fetch_account_summary(ib: IB) -> AccountMetrics:
    """
    Fetches raw account summary values from the TWS and delegates to Core.

    Args:
        ib: The connected IB client instance.

    Returns:
        Calculated AccountMetrics.
    """
    # reqAccountSummary or accountValues can be used.
    # accountValues() returns a list of AccountValue named tuples for the active account.
    raw_values = ib.accountValues()

    net_liq = 0.0
    total_margin = 0.0
    avail_funds = 0.0

    for val in raw_values:
        if val.tag == "NetLiquidation":
            net_liq = float(val.value)
        elif val.tag in {"FullInitMarginReq", "InitMarginReq"}:
            total_margin = float(val.value)
        elif val.tag == "AvailableFunds":
            avail_funds = float(val.value)

    return calculate_account_metrics(net_liq, total_margin, avail_funds)


async def query_ibkr_data(host: str, port: int, client_id: int) -> None:
    """
    Main orchestration function connecting to TWS and logging details.

    Args:
        host: TWS host IP address.
        port: TWS API port.
        client_id: Unique client ID for connection.
    """
    ib = IB()
    try:
        logger.info(
            "Connecting to TWS on %s:%d (Client ID: %d)...",
            host,
            port,
            client_id,
        )
        # Timeout connection attempt at 10 seconds
        await asyncio.wait_for(
            ib.connectAsync(host, port, clientId=client_id),
            timeout=10.0,
        )
        logger.info("Successfully connected to IBKR!")

        # 1. Query Account Metrics
        metrics = await fetch_account_summary(ib)
        logger.info("--- Account Metrics Summary ---")
        logger.info("Net Liquidation: %.2f", metrics.net_liquidation)
        logger.info("Total Margin Req: %.2f", metrics.total_margin)
        logger.info("Available Funds:  %.2f", metrics.available_funds)
        logger.info("Margin Cushion:   %.2f%%", metrics.cushion_ratio * 100.0)

        # 2. Query Open Positions
        positions: list[Position] = ib.positions()
        logger.info("--- Active Positions (%d) ---", len(positions))
        for pos in positions:
            logger.info(
                "Symbol: %s | Position: %s | Avg Cost: %.2f",
                pos.contract.symbol,
                pos.position,
                pos.averageCost,
            )

    except TimeoutError:
        logger.error("Connection attempt to TWS timed out.")
    except ConnectionRefusedError:
        logger.error(
            "Connection refused. Verify that TWS or Gateway is running and API is enabled."
        )
    except Exception as exc:
        logger.error(
            "An unexpected error occurred during IBKR operations: %s",
            exc,
            exc_info=True,
        )
    finally:
        ib.disconnect()
        logger.info("Disconnected from IBKR TWS.")


def run() -> None:
    """Loads configuration and executes the async query function."""
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    client_id = DEFAULT_CLIENT_ID

    if PROJECT_CONFIG_AVAILABLE:
        try:
            # Attempt to resolve config toml at project root
            config = load_config(Path(__file__).resolve().parents[3])
            host = config.tws.host
            port = config.tws.port
            client_id = config.tws.client_id
            logger.info("Loaded configuration from project config.toml")
        except Exception as exc:
            logger.warning(
                "Could not load project config, using defaults. Error: %s", exc
            )

    asyncio.run(query_ibkr_data(host, port, client_id))


if __name__ == "__main__":
    run()
