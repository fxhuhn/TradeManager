---
description: "Workflow for inspecting live production containers, logs, and system health via Dozzle MCP."
trigger: "/status"
---

# /status Workflow — Production Inspection Protocol

When the user asks to check the production system, investigate logs, or invokes `/status`:

1. **Direct MCP Calls Only (Zero Scratch Scripts)**:
   - Call Dozzle MCP tools natively via `call_mcp_tool` with `ServerName: "dozzle"`.
   - Strictly avoid creating helper scripts (e.g. `dozzle_client.py`) or executing ad-hoc Python snippets for MCP queries.
2. **Container & Host Check**:
   - Call `list_containers` to verify states of `trading-app` and `ibkr`.
   - Call `get_container_stats` for memory and CPU health.
3. **Log & Error Search**:
   - Call `search_container_logs` with targeted queries (`"error"`, `"warning"`, `"Timeout"`, etc.) for fast server-side filtering.
   - Call `get_container_logs` for chronological stream inspection.
4. **Synthesis**:
   - Provide a clean, structured status report covering containers, broker connection, open orders, and database state.
