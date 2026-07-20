---
description: "Sequentially triggers static analysis (vulture), vulnerability scans (bandit/pip-audit), and the test suite (pytest), returning a high-density health report."
trigger: "/full-review"
---

# /full-review Command Workflow

When the user invokes `/full-review` or requests a comprehensive codebase check, you must:

1. **Activate the Full-Review Skill**: Load and execute the instructions in [.agents/skills/full-review/SKILL.md](.agents/skills/full-review/SKILL.md).
2. **Execute Audit and Test Pipeline**:
   * **Dead Code Check**: Run `vulture . --exclude .venv,tests`
   * **Security Scan**: Run `bandit -r . -x tests`
   * **Dependency Scan**: Run `pip-audit -r requirements.txt`
   * **Test Suite Check**: Run `pytest tests/`
3. **Consolidate Results**: Synthesize the output from the steps above into a condensed, high-density health report.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
