#!/usr/bin/env python3
"""
Python Craftsman - 5-Gate Quality Pipeline Runner

Executes all 5 review gates sequentially:
1. Linting & Formatting Check (ruff)
2. Test Suite & Coverage Verification (pytest)
3. Dead Code & Architecture Audit (vulture)
4. Security Audit (bandit)
5. Architecture Synchronization Check (check_sync.py)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Repository root (3 levels up from .agents/skills/python-craftsman/scripts/)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent


def resolve_tool(tool_name: str) -> str | None:
    """Finds tool binary in system PATH or local .venv directory."""
    which_path = shutil.which(tool_name)
    if which_path:
        return which_path

    venv_bin = ROOT_DIR / ".venv" / "bin" / tool_name
    if venv_bin.is_file() and venv_bin.stat().st_mode & 0o111:
        return str(venv_bin)

    venv_scripts = ROOT_DIR / ".venv" / "Scripts" / f"{tool_name}.exe"
    if venv_scripts.is_file():
        return str(venv_scripts)

    return None


def get_python_interpreter() -> str:
    """Returns the Python interpreter path, prioritizing .venv if available."""
    venv_py = ROOT_DIR / ".venv" / "bin" / "python"
    if venv_py.is_file() and venv_py.stat().st_mode & 0o111:
        return str(venv_py)
    venv_py_win = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_py_win.is_file():
        return str(venv_py_win)
    return sys.executable


def run_gate(name: str, command: list[str], allow_missing_tool: bool = False) -> bool:
    """Runs a single quality gate command and returns True if passed."""
    print(f"\n{'=' * 60}")
    print(f"▶ Running {name}")
    print(f"  Command: {' '.join(command)}")
    print(f"{'=' * 60}")

    tool_binary = command[0]
    resolved = resolve_tool(tool_binary)
    if not resolved:
        if allow_missing_tool:
            print(
                f"⚠️  Tool '{tool_binary}' not found in PATH or .venv. Skipping optional gate."
            )
            return True
        print(f"❌ Error: Required tool '{tool_binary}' not found in PATH or .venv.")
        return False

    command[0] = resolved
    result = subprocess.run(command, cwd=ROOT_DIR, check=False)
    if result.returncode != 0:
        print(f"\n❌ Gate Failed: {name} (Exit Code: {result.returncode})")
        return False

    print(f"\n✅ Gate Passed: {name}")
    return True


def main() -> int:
    """Executes all 5 review gates in order."""
    print("=" * 60)
    print("🛠️  PYTHON CRAFTSMAN QUALITY PIPELINE")
    print("=" * 60)

    python_exec = get_python_interpreter()

    # Gate 1: Linting, Formatting & Type Check
    ruff_bin = resolve_tool("ruff")
    if ruff_bin:
        if not run_gate("Gate 1: Ruff Lint Check", ["ruff", "check", "."]):
            return 1
        if not run_gate(
            "Gate 1: Ruff Format Check", ["ruff", "format", "--check", "."]
        ):
            return 1
    else:
        print(
            "\n⚠️  Gate 1: ruff is not installed in current environment or .venv. Skipping."
        )

    # Mypy Strict Type Check
    if resolve_tool("mypy"):
        if not run_gate(
            "Gate 1: Mypy Strict Type Check",
            ["mypy", "--strict", "app"],
            allow_missing_tool=True,
        ):
            return 1

    # Gate 2: Test Suite Verification
    pytest_command = [
        python_exec,
        "-m",
        "pytest",
        "tests/",
        "-v",
        "--tb=short",
        "--cov=app",
        "--cov-report=term-missing",
        "--cov-fail-under=80",
    ]
    if not run_gate("Gate 2: Test Suite Verification (Pytest)", pytest_command):
        return 1

    # Gate 3: Architecture / Dead Code Audit (Vulture)
    if resolve_tool("vulture"):
        vulture_command = ["vulture", "app/", "vulture_whitelist.py"]
        if not run_gate(
            "Gate 3: Architecture Audit (Vulture)",
            vulture_command,
            allow_missing_tool=True,
        ):
            return 1

    # Gate 4: Security Audit (Bandit)
    if resolve_tool("bandit"):
        bandit_command = ["bandit", "-ll", "-x", "tests", "-r", "app"]
        if not run_gate(
            "Gate 4: Security Audit (Bandit)", bandit_command, allow_missing_tool=True
        ):
            return 1

    # Gate 5: Architecture Sync
    sync_script = (
        ROOT_DIR
        / ".agents"
        / "skills"
        / "architecture-sync"
        / "scripts"
        / "check_sync.py"
    )
    sync_command = [python_exec, str(sync_script)]
    if not run_gate("Gate 5: Architecture Sync Check", sync_command):
        return 1

    print("\n" + "=" * 60)
    print("🎉 ALL 5 CRAFTSMAN GATES PASSED SUCCESSFULLY!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
