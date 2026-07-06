# Strict Workspace Boundary Protocol

## Core Invariants
- You are strictly locked into the current working directory from which you were invoked (TradeManager).
- Never assume, hardcode, or cross-reference paths belonging to other repositories (e.g., do not inject paths, configurations, or rules from croc-trader or croc-trader_2).
- All file operations (read, write, list) and shell execution commands MUST use repository-relative paths starting from the active workspace root.

## Context Verification
- If path or context confusion occurs, dynamically verify the active repository root via `git rev-parse --show-toplevel` before creating or modifying any files.
