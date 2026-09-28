#!/usr/bin/env python3
"""
TradeManager - Dead Code & Cross-Reference AST Scanner

Performs a comprehensive AST-based cross-reference scan of the codebase to detect:
1. Functions and methods defined in app/ with 0 call or name references.
2. Classes defined in app/ with 0 references.
3. Classifies findings into:
   - CATEGORY A: Direct Deletion (private functions/methods with 0 references in app/ and tests/)
   - CATEGORY B: Deprecation Candidate (public functions/classes with 0 references in app/ runtime, but tested or documented)
   - CATEGORY C: Invariant / Whitelist (present in vulture_whitelist.py or IBKR event dispatchers)
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Workspace Root
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
APP_DIR = ROOT_DIR / "app"
TESTS_DIR = ROOT_DIR / "tests"
WHITELIST_FILE = ROOT_DIR / "vulture_whitelist.py"


@dataclass(frozen=True)
class CodeDefinition:
    name: str
    file_path: Path
    line_number: int
    def_type: str  # 'function', 'async_function', 'class', 'method'
    parent_class: str | None
    is_private: bool


@dataclass
class ScanResult:
    definition: CodeDefinition
    app_references: int
    tests_references: int
    is_whitelisted: bool
    is_in_architecture_md: bool
    category: str  # 'A_DIRECT_DELETE', 'B_DEPRECATE', 'C_WHITELIST_KEEP'
    reason: str


class DefinitionExtractor(ast.NodeVisitor):
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.definitions: list[CodeDefinition] = []
        self._current_class: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.definitions.append(
            CodeDefinition(
                name=node.name,
                file_path=self.file_path,
                line_number=node.lineno,
                def_type="class",
                parent_class=None,
                is_private=node.name.startswith("_"),
            )
        )
        old_class = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_func(node, is_async=True)

    def _record_func(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool
    ) -> None:
        # Ignore magic dunder methods (__init__, __str__, etc.)
        if node.name.startswith("__") and node.name.endswith("__"):
            self.generic_visit(node)
            return

        def_type = (
            "method"
            if self._current_class
            else ("async_function" if is_async else "function")
        )
        self.definitions.append(
            CodeDefinition(
                name=node.name,
                file_path=self.file_path,
                line_number=node.lineno,
                def_type=def_type,
                parent_class=self._current_class,
                is_private=node.name.startswith("_"),
            )
        )
        self.generic_visit(node)


class ReferenceCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.attributes: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.attributes.add(node.attr)
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        self.names.add(node.name)
        if node.asname:
            self.names.add(node.asname)
        self.generic_visit(node)


def load_whitelist() -> set[str]:
    """Extracts symbols from vulture_whitelist.py."""
    whitelisted: set[str] = set()
    if not WHITELIST_FILE.exists():
        return whitelisted

    content = WHITELIST_FILE.read_text(encoding="utf-8")
    for raw_line in content.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        # Handle _.attribute or plain symbol
        if stripped_line.startswith("_."):
            whitelisted.add(stripped_line[2:].strip())
        else:
            whitelisted.add(stripped_line)
    return whitelisted


def load_architecture_symbols() -> set[str]:
    """Extracts code symbols mentioned in architecture.md."""
    arch_file = ROOT_DIR / "architecture.md"
    if not arch_file.exists():
        return set()
    content = arch_file.read_text(encoding="utf-8")
    # Matches `symbol`
    return set(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*)`", content))


def collect_python_files(dir_path: Path) -> list[Path]:
    files: list[Path] = []
    if not dir_path.exists():
        return files
    for root, _, filenames in os.walk(dir_path):
        if "__pycache__" in root:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(Path(root) / fn)
    return files


def run_ast_scan() -> list[ScanResult]:
    whitelist = load_whitelist()
    arch_symbols = load_architecture_symbols()

    # Step 1: Collect all definitions in app/
    app_files = collect_python_files(APP_DIR)
    all_defs: list[CodeDefinition] = []

    for file_path in app_files:
        try:
            tree = ast.parse(
                file_path.read_text(encoding="utf-8"), filename=str(file_path)
            )
            extractor = DefinitionExtractor(file_path)
            extractor.visit(tree)
            all_defs.extend(extractor.definitions)
        except Exception as exc:
            print(f"Warning: Failed to parse {file_path}: {exc}", file=sys.stderr)

    # Step 2: Collect all references across app/
    app_collector = ReferenceCollector()
    for file_path in app_files:
        try:
            tree = ast.parse(
                file_path.read_text(encoding="utf-8"), filename=str(file_path)
            )
            app_collector.visit(tree)
        except Exception:
            pass

    # Step 3: Collect all references across tests/
    test_files = collect_python_files(TESTS_DIR)
    test_collector = ReferenceCollector()
    for file_path in test_files:
        try:
            tree = ast.parse(
                file_path.read_text(encoding="utf-8"), filename=str(file_path)
            )
            test_collector.visit(tree)
        except Exception:
            pass

    # Step 4: Evaluate each definition
    results: list[ScanResult] = []

    # Known callback methods hooked dynamically via IB events in callbacks.py
    known_event_handlers = {
        "on_order_status",
        "on_exec_details",
        "on_commission_report",
        "on_error",
        "on_connected",
        "on_disconnected",
        "register_all",
    }

    for defn in all_defs:
        is_whitelisted = defn.name in whitelist or defn.name in known_event_handlers
        is_in_arch = defn.name in arch_symbols

        # Count total references across source files (excluding its exact definition line)
        app_ref_count = 0
        for f in app_files:
            try:
                txt = f.read_text(encoding="utf-8")
                # Simple exact word match
                matches = len(re.findall(rf"\b{re.escape(defn.name)}\b", txt))
                if f == defn.file_path:
                    # subtract the definition occurrence
                    matches = max(0, matches - 1)
                app_ref_count += matches
            except Exception:
                pass

        test_ref_count = 0
        for f in test_files:
            try:
                txt = f.read_text(encoding="utf-8")
                matches = len(re.findall(rf"\b{re.escape(defn.name)}\b", txt))
                test_ref_count += matches
            except Exception:
                pass

        # Only process if 0 external app references
        if app_ref_count == 0:
            if is_whitelisted or defn.name in known_event_handlers:
                category = "C_WHITELIST_KEEP"
                reason = "Invariante: Explizit in vulture_whitelist.py oder IBKR Event Callback."
            elif defn.is_private:
                if test_ref_count == 0:
                    category = "A_DIRECT_DELETE"
                    reason = "Privat (_), 0 Aufrufe in app/ und 0 Aufrufe in tests/."
                else:
                    category = "B_DEPRECATE"
                    reason = "Privat (_), aber noch in tests/ referenziert. Vor Löschung Test prüfen."
            elif is_in_arch or test_ref_count > 0:
                category = "B_DEPRECATE"
                reason = (
                    "Öffentliche API (in architecture.md oder tests/ referenziert), "
                    "jedoch 0 Aufrufe in app/ Core-Pipeline."
                )
            else:
                category = "A_DIRECT_DELETE"
                reason = "Öffentliches Symbol mit 0 Referenzen in app/, tests/ und architecture.md."

            results.append(
                ScanResult(
                    definition=defn,
                    app_references=app_ref_count,
                    tests_references=test_ref_count,
                    is_whitelisted=is_whitelisted,
                    is_in_architecture_md=is_in_arch,
                    category=category,
                    reason=reason,
                )
            )

    return results


def print_report(results: list[ScanResult]) -> None:
    print("=" * 80)
    print("🔍 TRADEMANAGER DEAD CODE & CROSS-REFERENCE SCAN REPORT")
    print("=" * 80)

    cat_a = [r for r in results if r.category == "A_DIRECT_DELETE"]
    cat_b = [r for r in results if r.category == "B_DEPRECATE"]
    cat_c = [r for r in results if r.category == "C_WHITELIST_KEEP"]

    print(f"\n📊 Gefundene Kandidaten insgesamt: {len(results)}")
    print(f"   ├─ Kategorie A (Sofort löschbar):        {len(cat_a)}")
    print(f"   ├─ Kategorie B (Deprecation-Kandidaten): {len(cat_b)}")
    print(f"   └─ Kategorie C (Invariante / Whitelist): {len(cat_c)}")

    if cat_a:
        print("\n" + "─" * 80)
        print("🔴 KATEGORIE A: DIREKT LÖSCHBAR (0 Referenzen im Workspace)")
        print("─" * 80)
        for r in cat_a:
            rel_file = r.definition.file_path.relative_to(ROOT_DIR)
            print(
                f"  • [{r.definition.def_type.upper()}] {r.definition.name} ({rel_file}:{r.definition.line_number})"
            )
            print(f"    Grund: {r.reason}")

    if cat_b:
        print("\n" + "─" * 80)
        print("🟡 KATEGORIE B: DEPRECATION-KANDIDATEN (Öffentlich oder in Tests)")
        print("─" * 80)
        for r in cat_b:
            rel_file = r.definition.file_path.relative_to(ROOT_DIR)
            print(
                f"  • [{r.definition.def_type.upper()}] {r.definition.name} ({rel_file}:{r.definition.line_number})"
            )
            print(
                f"    App-Refs: {r.app_references} | Test-Refs: {r.tests_references} | In Arch: {r.is_in_architecture_md}"
            )
            print(f"    Grund: {r.reason}")

    if cat_c:
        print("\n" + "─" * 80)
        print("🟢 KATEGORIE C: INVARIANTE / WHITELIST (Nicht löschen!)")
        print("─" * 80)
        for r in cat_c:
            rel_file = r.definition.file_path.relative_to(ROOT_DIR)
            print(
                f"  • [{r.definition.def_type.upper()}] {r.definition.name} ({rel_file}:{r.definition.line_number})"
            )
            print(f"    Grund: {r.reason}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    scan_results = run_ast_scan()
    print_report(scan_results)
