---
description: "Run the 5-gate quality pipeline using the python-craftsman skill."
trigger: "/craft"
---

# /craft Command Workflow

When the user invokes `/craft` or requests a full quality gate pipeline on code changes, you must:

1. **Activate the Craftsman Skill**: Load and execute the instructions in [.agents/skills/python-craftsman/SKILL.md](.agents/skills/python-craftsman/SKILL.md).
2. **Execute All 5 Review Gates**: Run `python .agents/skills/python-craftsman/scripts/run_quality_gates.py` (or execute the 5 gates in order: Linting → Tests → Audit → Security → Architecture Sync).
3. **Report Gate Results**: For each gate, report pass/fail status. If any gate fails, stop and report the violations before proceeding.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
