---
description: "Workflow for running dry-run and What-If simulations on daily orders."
trigger: "/dry-run"
---

# /dry-run Command Workflow

When the user invokes `/dry-run` or requests to run a test, simulation, or verification of daily orders, you must execute the following 3-step diagnostic pipeline:

1. **Activate Skills**: Load and execute instructions in [.agents/skills/python-tester/SKILL.md](.agents/skills/python-tester/SKILL.md) and [.agents/skills/ibkr-agent/SKILL.md](.agents/skills/ibkr-agent/SKILL.md).
2. **Execute Diagnostic Pipeline**:
   - **Step 1 — TWS Connectivity**: Run `python scripts/check_tws.py` to verify Gateway/TWS socket connection and account access.
   - **Step 2 — Order & CSV Validation**: Run `python scripts/dry_run_validation.py` to audit daily `orders_YYYY_MM_DD.csv` formatting, bracket leg consistency, and capital downscaling math.
   - **Step 3 — Execution Simulation**: Run `python scripts/run_simulation.py` to perform dry-run `whatIf=True` margin cushion simulation on IBKR.
3. **Report Diagnostics**: Synthesize outputs into a high-density status report detailing connection status, validated trade groups, downscaled quantities, margin cushion usage, and any warning alerts.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
