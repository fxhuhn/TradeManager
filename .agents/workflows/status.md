---
description: "Workflow for inspecting live production containers, logs, PROD SQLite databases (trading.db), system health, and automated Croc vs. IBKR position reconciliation."
trigger: "/status"
---

# /status Workflow — Production Inspection Protocol

When the user asks to check the production system, investigate logs, or invokes `/status`:

1. **Protocol & Execution Paths**:
   - **Path A (Deterministic & Mandatory Default for Complete Status & Reconciliation)**: Run the consolidated plugin CLI:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py status
     ```
     *Benefits*: Executes the complete production inspection across 9 sections combining **Dozzle MCP** (containers & logs) and **Croc MCP** (direct PROD SQLite access to `trading.db`, `signals.db`, and `stocks.db`) concurrently in **< 1.5 seconds**.
     *Invariant*: For initial analysis, **ONLY run `status`**. Do NOT run multiple exploratory log streams or create temporary scratch files unless `status` explicitly flags an anomaly that requires deep dives.

   - **Path B (Dedicated Instant Position Reconciliation)**:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py reconcile
     ```
     *Benefits*: Performs exclusively the concurrent Croc-Trader (`signals.db`) vs. IBKR/TradeManager (`trading.db`) reality-check table in **< 0.5 seconds**.

   - **Path C (Dedicated PROD SQL Database Queries via Croc MCP)**:
     ```bash
     python3 .agents/plugins/croc-mcp/scripts/croc_cli.py orders --today
     python3 .agents/plugins/croc-mcp/scripts/croc_cli.py settlements
     python3 .agents/plugins/croc-mcp/scripts/croc_cli.py health
     ```
     *Benefits*: Directly queries the production SQLite database (`trading.db`) without guessing from logs or needing container shell access.

   - **Path D (Detailed Log Querying via Dozzle MCP)**:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs trading-app --since 60 --tail 100
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs ibkr --since 30 --tail 50
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs croc-trader --since 30 --tail 50
     ```
     *Rule*: ALWAYS provide `--tail` (default 100) when fetching container logs to prevent unbounded streaming delays.
   - Tool signatures, CLI commands and parameter schemas are defined in [.agents/plugins/dozzle-mcp/instructions.md](file:///Users/produktmanagement/Python/github/TradeManager/.agents/plugins/dozzle-mcp/instructions.md).

2. **Synthesis & Reporting**:
   - Deliver the results directly to the user based on the structured 9-section `status` output:
     1. Container states and health (`trading-app`, `ibkr`, `croc-trader`) with CPU & RAM (Dozzle MCP).
     2. **PROD SQL-Datenbanken & Systemgesundheit**: `trading.db`, `signals.db`, `stocks.db` existence, size, update time, and scheduler state (Croc MCP).
     3. Broker connection and last network event (Dozzle logs).
     4. Daily CSV target processing (`.bak` vs `.err`) and capital sizing adjustments.
     5. **Order-Pipeline Status (PROD SQL-DB `trading.db`)**: Ground-truth order counts by status (`PreSubmitted`, `Submitted`, `Filled`, `Cancelled`, `Error`), roles, strategies, and failed order details (Croc MCP).
     6. **Trades Settlement & Realisierter PnL (PROD SQL-DB `trading.db`)**: Today's settled count, realized Net PnL, commissions paid, slippage, and recent settlements list (Croc MCP).
     7. Real unhandled errors vs. filtered benign notices (Code 399 pre-market holds, data farm info, regular reconnects).
     8. Account equity, margin cushion, and Croc-Trader portfolio summary (invested capital, unrealized PnL).
     9. **Portfolio- & Positionsabgleich**: Exact breakdown per symbol: Croc SOLL vs. IBKR IST, Delta, Status (`🟢 MATCH`, `🟢 FUTURES`, `⏳ DOWNSIZED`, `⏳ SETTLED(TM)`, `ℹ️ UNASSIGNED`, `🔴 MISMATCH`), and Strategy references.
