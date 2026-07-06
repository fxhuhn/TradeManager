---
description: "Workflow for running dry-run and What-If simulations on daily orders."
trigger: "/dry-run"
---

# /dry-run Command Workflow

When the user invokes `/dry-run` or requests to run a test, simulation, or verification of an order file, you must:

1. **Activate the Tester Skill**: Load and execute the instructions in [.agent/skills/python-tester/SKILL.md](.agent/skills/python-tester/SKILL.md).
2. **Invoke the Dry-Run & Verification Protocol**: Refer to the dry-run priority guidelines under the "Dry-Run & Verification Protocol" section.
3. **Run Diagnostic Script**: Execute the appropriate python script under `scripts/` (such as `scripts/dry_run_validation.py` or other diagnostic/dry-run scripts) to get precise diagnostic output for the current database and CSV state.
4. **Return Results**: Return the complete diagnostic results and any captured notifier warning messages to the user.
5. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
