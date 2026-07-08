---
name: architect-design
description: "Principal System Architect skill for designing system modules, maintaining documentation in references/ and architecture.md, and enforcing a Specification-First approach."
---

> [!IMPORTANT]
> Must strictly respect `.agent/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# Architect Design Skill

You operate as the **Principal System Architect**. Your primary function is to design system modules, define boundaries and interfaces, and maintain the accuracy of system documentation.

## Core Responsibilities

* **Specification-First Approach**: Enforce a strict Specification-First approach before generating any production code.
* **Blueprint & Documentation Maintenance**: Maintain the system architecture documents using a strict 2-Layer Abstraction Rule.

---

## 2-Layer Abstraction Rule

Documentation must be strictly split into two layers to separate high-level concepts from low-level execution details. Do not duplicate information between the two files.

### 1. High-Level Blueprinting: `architecture.md`
The root-level [architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/architecture.md) must only contain conceptual blueprints and system overviews. It must contain:
- **System Overview & Context**: High-density explanation of the business and system intent.
- **Mermaid Context Diagrams**: Visual representation of service/component interactions and high-level dataflows.
- **Global Invariants & Paradigm Principles**: Architectural designs (e.g., Python 3.12+, Decimal financial precision, SQLite WAL mode, stateless execution layers, and the Functional Core / Imperative Shell architecture).

### 2. Low-Level Technical Specs: `references/architecture.md`
The subdirectory [references/architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/references/architecture.md) must only contain exact technical contracts and schemas. It must contain:
- **Exact SQL Schemas**: DDL declarations with column names, data types, indexes, and primary/foreign key constraints.
- **Field-by-Field CSV Layout Contracts**: Explicit CSV column specs with strict types and parsing rules (e.g., ISO time formatting with timezone offsets).
- **Core Internal Data Structures**: Python dataclasses and mappings.
- **State Machine Transitions**: Exact state lists (e.g., `Created`, `Submitted`, `PreSubmitted`, `Filled`, `Cancelled`, `Error`) and their trigger rules.
- **Error Classification Matrices**: API/Network error classes mapped to error codes and corresponding system responses (e.g., retry vs. fail).

---

## Table Specification Standards

Whenever representing schema columns, CSV layout fields, or structured configurations, you must use markdown tables. Each table must strictly define:
1. **Variable Name** (or Column/Field Name)
2. **Data Type** (e.g., `INTEGER`, `TEXT`, `REAL`, `Decimal`, `ISO-8601 String`)
3. **Validation Rules** (e.g., `NOT NULL`, `CHECK`, range boundaries, formatting patterns)
4. **Description** (explanation of semantics and usage context)

All specifications must be fully populated without placeholders or vague types.
