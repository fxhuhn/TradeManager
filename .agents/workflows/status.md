---
description: "Workflow for inspecting live production containers, logs, and system health via Dozzle MCP."
trigger: "/status"
---

# /status Workflow — Production Inspection Protocol

When the user asks to check the production system, investigate logs, or invokes `/status`:

1. **Protocol & Execution Paths**:
   - **Path A (Deterministic & Mandatory Default for Initial Analysis)**: Run the consolidated plugin CLI:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py status
     ```
     *Benefits*: Executes the complete production inspection (Containers & Resources, TWS Connection, Today's CSV Lifecycle, Order Pipeline States, Smart Noise/Error Classification, and Account/Margin Metrics) in **< 1 second**.
     *Invariant*: For initial analysis, **ONLY run `status`**. Do NOT run multiple exploratory log streams or create temporary scratch files unless `status` explicitly flags an anomaly that requires deep dives.
   - **Path B (Detailed Log Querying)**:
     ```bash
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs trading-app --since 60 --tail 100
     python3 .agents/plugins/dozzle-mcp/scripts/dozzle_cli.py logs ibkr --since 30 --tail 50
     ```
     *Rule*: ALWAYS provide `--tail` (default 100) when fetching container logs to prevent unbounded SSE streaming delays (especially on verbose containers like `ibkr`).
   - Tool signatures, CLI commands and parameter schemas are defined in [.agents/plugins/dozzle-mcp/instructions.md](file:///Users/produktmanagement/Python/github/TradeManager/.agents/plugins/dozzle-mcp/instructions.md).

2. **Synthesis & Reporting**:
   - Deliver the results directly to the user based on the structured 6-section `status` output:
     * Container states and health (`trading-app`, `ibkr`) with CPU & RAM.
     * Broker connection and last network event.
     * Daily CSV target processing (`.bak` vs `.err`) and capital sizing adjustments.
     * Order pipeline breakdown (e.g. `PreSubmitted`, `Submitted`, `Filled`, `Cancelled`, `Error`).
     * Real unhandled errors vs. filtered benign notices (Code 399 pre-market holds, data farm info, regular reconnects).
     * Account equity, margin cushion, and available funds.
