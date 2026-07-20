---
description: "Unified architecture orchestration pipeline trigger."
trigger: "/architect"
---

# /architect Command Workflow

When the user invokes `/architect` to coordinate complex rollouts or multi-file design changes, you must:

1. **Activate the Architect-Design Skill**: Load and execute the instructions in [.agents/skills/architect-design/SKILL.md](.agents/skills/architect-design/SKILL.md), including the **Multi-File Orchestration Protocol**.
2. **Execute Orchestration Pipeline**: Plan the sequence of structural changes, apply updates across multiple modules, and ensure all system components (code, tests, config, and docs) remain perfectly synchronized.
3. **Architecture Sync**: Run `python .agents/skills/architecture-sync/scripts/check_sync.py` to verify documentation is up to date.
4. **Verification**: Run `pytest tests/` and `ruff check .` to confirm structural and code integrity.
5. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.