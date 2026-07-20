---
name: full-review
description: "Lead Code Auditor skill for coordinating full sweeps across app/, tests/, and configuration files, synthesizing reports from python-auditor, python-tester, and python-security."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Full Review Skill

You operate as the **Lead Code Auditor**. Your mission is to perform comprehensive sweeps of the entire codebase and configuration to guarantee total quality, robustness, test coverage, and security compliance.

## Core Responsibilities

* **Comprehensive Sweeps**: Audit all files under `app/`, `tests/`, and configuration files (e.g. `pyproject.toml`, `config.toml`, `.pre-commit-config.yaml`).
* **Multi-Skill Synthesis**: Gather and synthesize findings from the sub-skills below.
* **Consolidated Reporting**: Present a unified, high-density health report using the report template defined in this skill.
* **Adherence to Core Rules**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md) and [.agents/rules/workspace.md](.agents/rules/workspace.md).

---

## Execution Pipeline

Execute these steps **in order** and collect results:

### Step 1: Static Analysis & Dead Code
```bash
vulture . --exclude .venv,tests
```

### Step 2: Security Vulnerability Scan
```bash
bandit -r . -x tests
pip-audit -r requirements.txt
```
Then apply the `python-security` skill analysis on the source code.

### Step 3: Code Quality Audit
Apply the `python-auditor` skill (Quality Pyramid scan) on all modules in `app/`.

### Step 4: Test Suite Verification
```bash
pytest tests/ -v --tb=short
```
Then apply the `python-tester` skill to assess test coverage and edge-case robustness.

### Step 5: Architecture Sync Validation
```bash
python .agents/skills/architecture-sync/scripts/check_sync.py
```

---

## Consolidated Report Template

Output a single Markdown report using this exact structure:

```markdown
# 🏥 CODEBASE HEALTH REPORT

## Executive Summary
**Overall Health:** [CRITICAL / WARNING / HEALTHY]
**Date:** YYYY-MM-DD

| Dimension        | Status | Details                    |
|------------------|--------|----------------------------|
| Dead Code        | ✅/❌  | _vulture findings count_   |
| Security         | ✅/❌  | _bandit/pip-audit summary_ |
| Code Quality     | ✅/❌  | _auditor score + layer_    |
| Test Coverage    | ✅/❌  | _pass/fail + coverage %_   |
| Architecture Sync| ✅/❌  | _sync check result_        |

## Detailed Findings

### 🔒 Security (from python-security)
_Risk level, critical findings, remediation priorities_

### 📊 Code Quality (from python-auditor)
_Quality layer reached, top violations, metric thresholds_

### 🧪 Test Coverage (from python-tester)
_Pass rate, missing edge cases, coverage gaps_

### 🗑️ Dead Code (from vulture)
_Unused functions/classes/imports_

### 📐 Architecture Sync
_Missing documentation for public components_

## Priority Action Items
1. [CRITICAL] ...
2. [HIGH] ...
3. [MEDIUM] ...
```

---

## Prioritization Rules

When consolidating findings across sub-skills, apply this priority order:
1. **Security vulnerabilities** (SQL injection, float-for-money, pickle) — always first
2. **Correctness failures** (broken tests, bare except, side effects in core) — second
3. **Dead code / unused imports** — third
4. **Maintainability gaps** (missing types, DRY violations) — fourth
5. **Changeability concerns** (tight coupling, missing abstractions) — last
