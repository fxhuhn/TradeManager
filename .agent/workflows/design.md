---
description: "Run code style and UX design using the python-designer skill."
trigger: "/design"
---

# /design Command Workflow

When the user invokes `/design` or requests a creative design review/implementation, you must:

1. **Activate the Designer Skill**: Load and execute the instructions in [.agent/skills/python-designer/SKILL.md](.agent/skills/python-designer/SKILL.md).
2. **Design Interface & UX**: Integrate terminal dashboards using `rich`, analytics optimizations with `DuckDB`, and document the life cycle of information using Mermaid diagrams.
3. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
