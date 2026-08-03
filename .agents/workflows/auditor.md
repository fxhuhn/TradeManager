---
description: "Run code quality audit using the python-auditor skill."
trigger: "/auditor"
---

# /auditor Command Workflow

When the user invokes `/auditor` or requests a code quality audit, you must:

1. **Activate the Auditor Skill**: Load and execute the instructions in [.agents/skills/python-auditor/SKILL.md](.agents/skills/python-auditor/SKILL.md).
2. **Review Selected Code**: Scan the code against the Quality Pyramid (Correctness -> Readability -> Maintainability -> Changeability) defined in the skill.
3. **Generate Audit Report**: Produce the structured report with scores, violation log, metrics assessment, and refactoring orders as detailed in the skill.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.