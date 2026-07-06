---
description: "Unified architecture orchestration pipeline trigger."
trigger: "/architect"
---

# /architect Command Workflow

When the user invokes `/architect` to coordinate complex rollouts or multi-file design changes, you must:

1. **Activate the Architect-Workflow Skill**: Load and execute the instructions in [.agent/skills/architect-workflow/SKILL.md](.agent/skills/architect-workflow/SKILL.md).
2. **Execute Orchestration Pipeline**: Plan the sequence of structural changes, apply updates across multiple modules, and ensure all system components (code, tests, config, and docs) remain perfectly synchronized.
3. **Verification**: Run local verification checks to confirm structural and architectural integrity.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.