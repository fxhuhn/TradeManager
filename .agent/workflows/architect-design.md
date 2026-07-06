---
description: "Principal System Architect & Technical Writer workflow for maintaining architecture documents and diagrams in references/ and architecture.md."
trigger: "/architect-design"
---

# /architect-design Command Workflow

When the user invokes `/architect-design` with structural or interface updates, you must:

1. **Activate the Architect-Design Skill**: Load and execute the instructions in [.agent/skills/architect-design/SKILL.md](.agent/skills/architect-design/SKILL.md).
2. **Synchronize Documentation**: Automatically modify or generate the relevant documentation files inside the `references/` directory and keep the root `architecture.md` fully aligned with the updates.
3. **Strict Boundaries**: Adhere to [.agent/rules/workspace.md](.agent/rules/workspace.md). Ensure all references and operations are contained within the repository workspace.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
