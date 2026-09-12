---
description: "Workflow for inspecting live production containers, logs, and system health via Dozzle MCP."
trigger: "/status"
---

# /status Workflow — Production Inspection Protocol

When the user asks to check the production system, investigate logs, or invokes `/status`:

1. **Protocol & Invariants**:
   - Follow the mandatory MCP invariant in `AGENTS.md` (direct `call_mcp_tool` calls with `ServerName: "dozzle"`, zero browser automation, zero scratch scripts, fail-stop on error).
   - Tool signatures and parameter schemas are defined in [.agents/plugins/dozzle-mcp/instructions.md](file:///Users/produktmanagement/Python/github/TradeManager/.agents/plugins/dozzle-mcp/instructions.md).

2. **Container & Host Check**:
   - Call `list_containers` to identify the host and verify running states of `trading-app` and `ibkr`.
   - Call `get_container_stats` for `trading-app` and `ibkr` to check memory usage and CPU load.

3. **Log & Error Search**:
   - Call `search_container_logs` on `trading-app` with targeted queries (`"error"`, `"warning"`, `"Timeout"`, `since_minutes: 30`) for fast server-side filtering.
   - Call `get_container_logs` on `trading-app` (`since_minutes: 30`) for chronological stream inspection of recent events.

4. **Synthesis & Reporting**:
   - Provide a clean, structured status report covering:
     * Container states and health (`trading-app`, `ibkr`).
     * Resource usage (CPU %, RAM MB/%).
     * Broker connection and account metric synchronization.
     * Recent errors, warnings, or order lifecycle anomalies.


