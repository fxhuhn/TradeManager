#!/bin/bash
# lint_check.sh - Autonomous Lint and Style Checker for the IBKR Agent Skill.
# Runs Ruff lint check and formatting check.

# Find the repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Define paths to ruff
RUFF_BIN="ruff"
if [ -f "$REPO_ROOT/.venv/bin/ruff" ]; then
    RUFF_BIN="$REPO_ROOT/.venv/bin/ruff"
fi

echo "========================================="
echo "Running Linting & Formatting Audit..."
echo "Target directory: $SKILL_DIR"
echo "Using ruff binary: $RUFF_BIN"
echo "========================================="

# Run ruff check
echo "Step 1: Running Ruff Linter check..."
"$RUFF_BIN" check "$SKILL_DIR"
RUFF_CHECK_STATUS=$?

# Run ruff format check
echo "Step 2: Running Ruff Formatter verification..."
"$RUFF_BIN" format --check "$SKILL_DIR"
RUFF_FORMAT_STATUS=$?

if [ $RUFF_CHECK_STATUS -eq 0 ] && [ $RUFF_FORMAT_STATUS -eq 0 ]; then
    echo "========================================="
    echo "✅ SUCCESS: All linting and formatting checks passed!"
    echo "========================================="
    exit 0
else
    echo "========================================="
    echo "❌ FAILURE: Linting or formatting checks failed."
    echo "Check Status: $RUFF_CHECK_STATUS"
    echo "Format Status: $RUFF_FORMAT_STATUS"
    echo "========================================="
    exit 1
fi
