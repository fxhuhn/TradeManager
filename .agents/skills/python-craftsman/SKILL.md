---
name: python-craftsman
description: "Master Python developer skill orchestrating 5 review gates (linting, tests, audit, security, architecture sync) to enforce quality standards from python.md."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Python Craftsman Skill

You operate as the **Master Craftsman** — the final quality gatekeeper before any code change is considered complete. All coding standards are defined in [python.md](.agents/rules/python.md) (auto-loaded as a rule). This skill does **not** duplicate those rules; it orchestrates the verification pipeline.

## When to Activate

This skill is triggered whenever code is written, modified, or refactored. It ensures every change passes through all 5 review gates before being declared complete.

## Automated 5-Gate Pipeline Execution

To run all 5 gates synchronously in a single command before committing or concluding tasks:
```bash
python .agents/skills/python-craftsman/scripts/run_quality_gates.py
```

---

## Delegated Review Gates

Before finalizing any task or committing changes, you must pass the code through the following validation gates **in order** (either via `python .agents/skills/python-craftsman/scripts/run_quality_gates.py` or step-by-step):

### 🚀 Gate 1: Linting & Style Check
Run ruff check and formatting verification to ensure compliance with [python.md](.agents/rules/python.md) style rules:
```bash
ruff check .
ruff format --check .
```

### 🧪 Gate 2: Test Suite Verification
Trigger the `python-tester` skill (workflow `/test`) to design and execute robust unit/integration tests:
```bash
pytest tests/ -v --tb=short --cov=app --cov-report=term-missing --cov-fail-under=80
```

### 🔍 Gate 3: Architecture Audit
Trigger the `python-auditor` skill (workflow `/auditor`) to run a complete Quality Pyramid audit (Correctness → Readability → Maintainability → Changeability) on your changes.

### 🛡️ Gate 4: Security & Dependency Audit
Execute static security analysis (`bandit`) and dependency vulnerability auditing (`pip-audit -r requirements.txt`). Trigger the `python-security` skill (workflow `/security`) to run a zero-trust audit for precision loss (using Decimal instead of float), injection risks, and serialization vulnerabilities.

### 📐 Gate 5: Architecture Sync
Verify all public classes and functions are documented:
```bash
python .agents/skills/architecture-sync/scripts/check_sync.py
```

---

## Gate Failure Protocol & Pre-Commit Invariant

- **Pre-Commit Enforcement**: The Git pre-commit hook enforces `pytest` and `architecture-sync-check`. A commit will be rejected on Git level if any test fails.
- **Any gate failure** blocks the task from being marked complete.
- Fix violations before re-running the failed gate.
- **Strict Conciseness**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.
