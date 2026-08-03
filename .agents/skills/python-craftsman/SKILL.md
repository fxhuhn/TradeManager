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

---

## Delegated Review Gates

Before finalizing any task or committing changes, you must pass the code through the following validation gates **in order**:

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

### 🛡️ Gate 4: Security Audit
Trigger the `python-security` skill (workflow `/security`) to run a zero-trust audit for precision loss (using Decimal instead of float), injection risks, and serialization vulnerabilities.

### 📐 Gate 5: Architecture Sync
Verify all public classes and functions are documented:
```bash
python .agents/skills/architecture-sync/scripts/check_sync.py
```

---

## Gate Failure Protocol

- **Any gate failure** blocks the task from being marked complete.
- Fix violations before re-running the failed gate.
- **Strict Conciseness**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.
