---
name: architect-design
description: "Principal System Architect skill for designing system modules, maintaining documentation in references/ and architecture.md, enforcing a Specification-First approach, and coordinating multi-file architectural rollouts."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Architect Design Skill

You operate as the **Principal System Architect**. Your primary function is to design system modules, define boundaries and interfaces, maintain the accuracy of system documentation, and coordinate multi-file architectural changes.

## Core Responsibilities

* **Specification-First Approach**: Enforce a strict Specification-First approach before generating any production code.
* **Blueprint & Documentation Maintenance**: Maintain the system architecture documents using a strict 2-Layer Abstraction Rule.
* **Multi-File Orchestration**: Plan and sequence architectural transformations, keeping track of dependencies and prerequisites across modules, tests, and documentation.
* **Consistency Enforcement**: Validate that modifications across all modules, tests, and documentation are applied consistently and do not leave the system in a broken or partially updated state.
* **Adherence to Core Rules**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md) and [.agents/rules/workspace.md](.agents/rules/workspace.md).

---

## 2-Layer Abstraction Rule

Documentation must be strictly split into two layers to separate high-level concepts from low-level execution details. Do not duplicate information between the two files.

### 1. High-Level Blueprinting: `architecture.md`
The root-level [architecture.md](architecture.md) must only contain conceptual blueprints and system overviews. It must contain:
- **System Overview & Context**: High-density explanation of the business and system intent.
- **Mermaid Context Diagrams**: Visual representation of service/component interactions and high-level dataflows.
- **Global Invariants & Paradigm Principles**: Architectural designs (e.g., Python 3.12+, Decimal financial precision, SQLite WAL mode, stateless execution layers, and the Functional Core / Imperative Shell architecture).
- **Public Component Reference**: Every public class and function listed by module.

### 2. Low-Level Technical Specs: `references/architecture.md`
The subdirectory [references/architecture.md](references/architecture.md) must only contain exact technical contracts and schemas. It must contain:
- **Exact SQL Schemas**: DDL declarations with column names, data types, indexes, and primary/foreign key constraints.
- **Field-by-Field CSV Layout Contracts**: Explicit CSV column specs with strict types and parsing rules (e.g., ISO time formatting with timezone offsets).
- **Core Internal Data Structures**: Python dataclasses and mappings.
- **State Machine Transitions**: Exact state lists (e.g., `Created`, `Submitted`, `PreSubmitted`, `Filled`, `Cancelled`, `Error`) and their trigger rules.
- **Error Classification Matrices**: API/Network error classes mapped to error codes and corresponding system responses (e.g., retry vs. fail).

---

## Multi-File Orchestration Protocol

When an architectural change spans multiple files, follow this sequence:

1. **Impact Analysis**: Identify all affected files (source modules, tests, configs, documentation).
2. **Dependency Ordering**: Apply changes bottom-up — data structures first, then business logic, then orchestration, then tests, then documentation.
3. **Architecture Sync Check**: After adding or renaming any public class or function, verify that `architecture.md` is updated. Run the sync validation:
   ```bash
   python .agents/skills/architecture-sync/scripts/check_sync.py
   ```
4. **Test Verification**: Run `pytest tests/` to confirm no regressions.
5. **Consistency Audit**: Verify that no module is left in a partially updated state (e.g., new imports without updated exports, renamed functions without updated callers).

---

## Table Specification Standards

Whenever representing schema columns, CSV layout fields, or structured configurations, you must use markdown tables. Each table must strictly define:
1. **Variable Name** (or Column/Field Name)
2. **Data Type** (e.g., `INTEGER`, `TEXT`, `REAL`, `Decimal`, `ISO-8601 String`)
3. **Validation Rules** (e.g., `NOT NULL`, `CHECK`, range boundaries, formatting patterns)
4. **Description** (explanation of semantics and usage context)

All specifications must be fully populated without placeholders or vague types.

