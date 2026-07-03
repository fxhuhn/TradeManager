---
description: "Run code quality audit using the python-auditor skill."
trigger: "/audit"
---

# /audit Command Workflow

When the user invokes `/audit` or request a code quality audit, you must:

1. **Activate the Auditor Skill**: Load and execute the instructions in [.agent/skills/python-auditor/SKILL.md](.agent/skills/python-auditor/SKILL.md).
2. **Review Selected Code**: Scan the code against the Quality Pyramid (Correctness -> Readability -> Maintainability -> Changeability) defined in the skill.
3. **Generate Audit Report**: Produce the structured report with scores, violation log, metrics assessment, and refactoring orders as detailed in the skill.