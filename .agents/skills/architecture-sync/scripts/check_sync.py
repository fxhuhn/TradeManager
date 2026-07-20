"""
Verification script to ensure public code definitions are documented.

This script parses all Python files in the source directories, extracts public
classes and functions using AST, and verifies that they are present in the
root-level architecture.md file.
"""

import ast
import os
import sys
from pathlib import Path


def get_source_directory(project_root: Path) -> Path | None:
    """
    Determines the source directory (app/ or src/) in the workspace.

    Args:
        project_root: Path to the workspace root.

    Returns:
        Path to the source directory or None if not found.
    """
    app_dir = project_root / "app"
    src_dir = project_root / "src"

    if app_dir.exists() and app_dir.is_dir():
        return app_dir
    if src_dir.exists() and src_dir.is_dir():
        return src_dir
    return None


def extract_public_definitions(source_dir: Path) -> set[str]:
    """
    Recursively scans the directory for public classes and functions using AST.

    Args:
        source_dir: Path to the source directory.

    Returns:
        A set of public class and function names.
    """
    public_definitions: set[str] = set()

    for root, _, files in os.walk(source_dir):
        # Skip pycache or tests folders if nested
        if "__pycache__" in root or "tests" in root:
            continue

        for file in files:
            if not file.endswith(".py") or file.startswith("test_"):
                continue

            file_path = Path(root) / file
            try:
                with open(file_path, encoding="utf-8") as file_handle:
                    source_code = file_handle.read()

                tree = ast.parse(source_code, filename=str(file_path))

                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        if not node.name.startswith("_"):
                            public_definitions.add(node.name)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not node.name.startswith("_"):
                            public_definitions.add(node.name)

            except Exception as exc:
                print(
                    f"Warning: Failed to parse file {file_path}. Error: {exc}",
                    file=sys.stderr,
                )

    return public_definitions


def verify_synchronization(
    project_root: Path, public_definitions: set[str]
) -> list[str]:
    """
    Verifies that all public definitions are present in architecture.md.

    Args:
        project_root: Path to the workspace root.
        public_definitions: Set of public names to check.

    Returns:
        A list of missing definitions.
    """
    arch_md_path = project_root / "architecture.md"

    if not arch_md_path.exists():
        print(
            f"Error: Project-root architecture.md not found at {arch_md_path.absolute()}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(arch_md_path, encoding="utf-8") as file_handle:
        doc_content = file_handle.read()

    missing: list[str] = []
    for name in sorted(public_definitions):
        if name not in doc_content:
            missing.append(name)

    return missing


def main() -> None:
    """Orchestrates parsing, extraction, and verification of code sync."""
    script_dir = Path(__file__).resolve().parent
    # Project root is 4 levels up: scripts -> architecture-sync -> skills -> .agents -> repo_root
    project_root = script_dir.parents[3]

    source_dir = get_source_directory(project_root)
    if not source_dir:
        print(
            "Error: Neither 'app/' nor 'src/' directory found at project root.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Scanning source directory: {source_dir.absolute()}")
    public_defs = extract_public_definitions(source_dir)
    print(f"Found {len(public_defs)} public classes and functions.")

    missing = verify_synchronization(project_root, public_defs)

    if missing:
        print(
            "\n❌ Error: The following public components are not documented in architecture.md:"
        )
        for name in missing:
            print(f"  - {name}")
        print(
            "\nPlease document these components in the project-root architecture.md file."
        )
        sys.exit(1)

    print("\n✅ Success: All public components are documented in architecture.md!")
    sys.exit(0)


if __name__ == "__main__":
    main()
