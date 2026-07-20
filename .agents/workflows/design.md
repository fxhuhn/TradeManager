---
description: "Run code style, Telegram formatting, and diagram design using the python-designer skill."
trigger: "/design"
---

# /design Command Workflow

When the user invokes `/design` or requests a creative design review/implementation, you must:

1. **Activate the Designer Skill**: Load and execute the instructions in [.agents/skills/python-designer/SKILL.md](.agents/skills/python-designer/SKILL.md).
2. **Design Interface & UX**: Design Telegram notification templates, structured logging output, and Mermaid architecture diagrams aligned with the TradeManager system.
3. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
