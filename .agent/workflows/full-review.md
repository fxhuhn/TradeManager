---
description: "Run vulture, bandit, and pip-audit checks sequentially."
trigger: "/audit"
---

# /audit Command Workflow

When the user invokes `/audit` or requests a full review, you must sequentially run the following local CLI checks:

1. **Dead Code Check**: Run `vulture . --exclude .venv,tests`.
2. **Security Vulnerability Scan**: Run `bandit -r . -x tests`.
3. **Dependency Vulnerability Scan**: Run `pip-audit -r requirements.txt`.

## Output Requirements
- **Anomalies**: Clearly list any dead code, security flaws, or vulnerable packages.
- **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
