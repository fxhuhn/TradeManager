---
description: "Principal System Architect & Technical Writer workflow for maintaining architecture documents and diagrams in references/ and architecture.md."
trigger: "/architect-design"
---

# /architect-design Command Workflow

When the user invokes `/architect-design` or requests structural or interface updates, you must:

1. **Activate the Architect-Design Skill**: Load and execute the instructions in [.agent/skills/architect-design/SKILL.md](.agent/skills/architect-design/SKILL.md).
2. **Auto-generate/Modify Specifications**: Automatically modify or generate the relevant documentation files inside the `references/` directory and keep the root `architecture.md` fully aligned.
3. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
