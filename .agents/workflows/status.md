---
description: "Workflow for inspecting live production containers, logs, system health, and automated Croc vs. IBKR position reconciliation."
trigger: "/status"
---

# /status Workflow — Production Inspection Protocol

When the user asks to check the production system, investigate logs, or invokes `/status`:

1. **Protocol & Execution Paths**:
   - **Path A (Deterministic & Mandatory Default for Complete Status & Reconciliation)**: Run the consolidated plugin CLI:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py status
     ```
     *Benefits*: Executes the complete production inspection across 7 sections (Containers & Resources, TWS Gateway Socket, Today's CSV Lifecycle, Order Pipeline States, Smart Noise/Error Classification, Account/Margin Metrics, and **Live Portfolio Reconciliation with Croc-Trader**) concurrently in **< 0.5 seconds**.
     *Invariant*: For initial analysis, **ONLY run `status`**. Do NOT run multiple exploratory log streams or create temporary scratch files unless `status` explicitly flags an anomaly that requires deep dives.

   - **Path B (Dedicated Instant Position Reconciliation)**:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py reconcile
     ```
     *Benefits*: Performs exclusively the concurrent Croc-Trader (`signals.db`) vs. IBKR/TradeManager (`trading.db`) reality-check table in **< 0.3 seconds**.

   - **Path C (Detailed Log Querying)**:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs trading-app --since 60 --tail 100
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs ibkr --since 30 --tail 50
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs croc-trader --since 30 --tail 50
     ```
     *Rule*: ALWAYS provide `--tail` (default 100) when fetching container logs to prevent unbounded streaming delays.
   - Tool signatures, CLI commands and parameter schemas are defined in [.agents/plugins/dozzle-mcp/instructions.md](file:///Users/produktmanagement/Python/github/TradeManager/.agents/plugins/dozzle-mcp/instructions.md).

2. **Synthesis & Reporting**:
   - Deliver the results directly to the user based on the structured 7-section `status` output:
     1. Container states and health (`trading-app`, `ibkr`, `croc-trader`) with CPU & RAM.
     2. Broker connection and last network event.
     3. Daily CSV target processing (`.bak` vs `.err`) and capital sizing adjustments.
     4. Order pipeline breakdown (e.g. `PreSubmitted`, `Submitted`, `Filled`, `Cancelled`, `Error`).
     5. Real unhandled errors vs. filtered benign notices (Code 399 pre-market holds, data farm info, regular reconnects).
     6. Account equity, margin cushion, and available funds.
     7. **Portfolio- & Positionsabgleich**:
        - Exact breakdown per symbol: Croc SOLL vs. IBKR IST, Delta, Status (`🟢 MATCH`, `🟢 FUTURES`, `⏳ DOWNSIZED`, `⏳ SETTLED(TM)`, `ℹ️ UNASSIGNED`, `🔴 MISMATCH`), and Strategy references.
