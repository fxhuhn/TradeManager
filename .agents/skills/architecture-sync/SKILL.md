---
name: architecture-sync
description: "Ensures the project-root architecture.md stays fully synchronized with public code components in the codebase."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Architecture Sync Skill

This skill enforces synchronization between the active codebase structure and the system documentation (`architecture.md` at the project root).

## Guidelines & Rules

### 1. Triggers
This skill is triggered whenever any of the following code modifications occur in `app/` (or `src/` if present):
* Adding, renaming, or deleting public Class definitions.
* Adding, renaming, or deleting public Function definitions (including async functions).
* Creating or deleting source code modules or database schemas.

### 2. Constraints
* Before declaring any implementation task complete, you **must** verify that any newly added public Class or Function is documented in the project-root [architecture.md](architecture.md) file.
* This documentation check is case-sensitive and scans for the exact class/function name within the documentation text.
* The original [references/architecture.md](references/architecture.md) is **read-only**; all modifications must be made strictly to the root [architecture.md](architecture.md) file.
* Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md). Minimize token consumption. Restrict explanations to the absolute technical core.

### 3. Pre-Commit Validation Hook
Every commit runs a local validation script to audit synchronization. To run this check manually:
```bash
poetry run python .agents/skills/architecture-sync/scripts/check_sync.py
```
Or if poetry is not used, run via:
```bash
python .agents/skills/architecture-sync/scripts/check_sync.py
```
This check must exit with `0` for any commit to succeed.
