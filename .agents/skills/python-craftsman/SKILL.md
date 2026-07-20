---
name: python-craftsman
description: "Master Python developer skill enforcing strict rules for Python 3.12+, async-first design, type hints, clean naming, early returns, complexity thresholds, and Quality review gates."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Python Craftsman Skill

This skill enforces high-end software craftsmanship guidelines for all Python code. It is based entirely on the strict laws defined in `python.md`.

## Core Coding Guidelines

### 1. Modern Python & Async Design
* **Version**: Use Python 3.12+ syntax exclusively.
* **Async**: Design code to run asynchronously using `asyncio` for all I/O and network operations. Avoid blocking calls in async event loops.

### 2. Type Hinting & Data Structures
* **Strict Typing**: All function arguments, return values, and class attributes **MUST** have type hints. No bare `Any` type.
* **Dataclasses**: Use `@dataclass(frozen=True)` for immutable internal business objects to guarantee data integrity.
* **TypedDict**: Use `TypedDict` for data exchange boundaries (e.g. config parsing, external JSON inputs/outputs).
* **Modern Syntax**: Use native types (e.g., `list[str]` instead of `List[str]`, `str | int` instead of `Union[str, int]`).

### 3. Clean Code & Intention-Revealing Naming
* **No Abbreviations**: Variables, functions, and parameters must be fully descriptive. No `ctx`, `val`, `res`, `idx`. Use `context`, `value`, `result`, `iteration_index`. (Allowed exceptions: `df`, `db`, `avg`, `qty`, `pnl`).
* **30-Second Rule**: A developer must understand *what* a function does and *why* in 30 seconds.
* **Early-Return Pattern**: Use guard clauses at the beginning of functions to handle edge cases and invalid states early. Keep the happy path at the lowest indentation level.

### 4. Complexity Constraints
* **Indentation Depth**: Max 3 levels of indentation.
* **Cognitive Complexity**: Max 15 per function.
* **Cyclomatic Complexity**: Max 10 per function.
* **Function Length**: Functions must fit on one screen (max ~50 lines).

### 5. Functional Core / Imperative Shell
* **Functional Core**: Pure, deterministic calculations. Zero side effects (no logging, no I/O, no DB access, no network, no `datetime.now()`). Easily testable without mocks.
* **Imperative Shell**: Handles inputs/outputs, persistence, logging, network calls, and validates constraints at the boundaries before data enters the core.

### 6. Conciseness
* **Strict Conciseness**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.

---

## Delegated Review Gates

Before finalizing any task or committing changes, you must pass the code through the following validation gates:

### 🚀 Gate 1: Linting & Style Check
Run ruff check and formatting verification to ensure compliance with style rules:
```bash
ruff check .
ruff format --check .
```

### 🧪 Gate 2: Test Suite Verification
Trigger the `python-tester` skill (workflow `/test`) to design and execute robust unit/integration tests and run pytest to verify correctness:
```bash
pytest tests/
```

### 🔍 Gate 3: Architecture Audit
Trigger the `python-auditor` skill (workflow `/auditor`) to run a complete Quality Pyramid audit (Correctness -> Readability -> Maintainability -> Changeability) on your changes.

### 🛡️ Gate 4: Security Audit
Trigger the `python-security` skill (workflow `/security`) to run a zero-trust audit for precision loss (using Decimal instead of float), injection risks, and serialization vulnerabilities.

### 📐 Gate 5: Architecture Sync
Verify all public classes and functions are documented:
```bash
python .agents/skills/architecture-sync/scripts/check_sync.py
```

