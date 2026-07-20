#!/bin/bash
# sec_audit.sh - Autonomous Security Auditor for the IBKR Agent Skill.
# Runs Bandit on the skill's files to check for vulnerabilities.

# Find the repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Define paths to bandit
BANDIT_BIN="bandit"
if [ -f "$REPO_ROOT/.venv/bin/bandit" ]; then
    BANDIT_BIN="$REPO_ROOT/.venv/bin/bandit"
fi

echo "========================================="
echo "Running Security Audit (Bandit)..."
echo "Target directory: $SKILL_DIR"
echo "Using bandit binary: $BANDIT_BIN"
echo "========================================="

# Run bandit
"$BANDIT_BIN" -r "$SKILL_DIR"
BANDIT_STATUS=$?

if [ $BANDIT_STATUS -eq 0 ]; then
    echo "========================================="
    echo "✅ SUCCESS: Security audit passed! No issues found."
    echo "========================================="
    exit 0
else
    echo "========================================="
    echo "❌ FAILURE: Security vulnerabilities or issues detected by Bandit."
    echo "Bandit Status: $BANDIT_STATUS"
    echo "========================================="
    exit 1
fi
