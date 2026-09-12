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

All gate commands, flags, and tool resolutions are centrally and authoritatively managed by [run_quality_gates.py](scripts/run_quality_gates.py). Before finalizing any task or creating a commit, code must pass all 5 gates in order:

- **🚀 Gate 1: Linting, Formatting & Type Safety**: Verifies zero lint errors, formatting compliance, and strict Mypy typing against [python.md](.agents/rules/python.md).
- **🧪 Gate 2: Test Suite Verification**: Runs the comprehensive test suite with $\ge 80\%$ coverage via the `python-tester` skill (workflow `/test`).
- **🔍 Gate 3: Architecture & Dead Code Audit**: Runs dead code and quality checks via the `python-auditor` skill (workflow `/auditor`).
- **🛡️ Gate 4: Security & Dependency Audit**: Scans for vulnerabilities, precision issues (zero float), and package risks via the `python-security` skill (workflow `/security`).
- **📐 Gate 5: Architecture Sync**: Validates that all public classes and functions in `app/` are documented in `architecture.md` via the `architecture-sync` skill.


---

## Gate Failure Protocol & Pre-Commit Invariant

- **Pre-Commit Enforcement**: The Git pre-commit hook enforces `pytest` and `architecture-sync-check`. A commit will be rejected on Git level if any test fails.
- **Any gate failure** blocks the task from being marked complete.
- Fix violations before re-running the failed gate.
- **Strict Conciseness**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.
