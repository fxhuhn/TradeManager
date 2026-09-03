---
description: "Unified architecture design, multi-file orchestration, and documentation sync pipeline trigger."
trigger: "/architect"
---

# /architect Command Workflow

When the user invokes `/architect` to coordinate complex rollouts, multi-file structural changes, or maintain system documentation/diagrams, you must:

1. **Activate the Architect-Design Skill**: Load and execute the instructions in [.agents/skills/architect-design/SKILL.md](.agents/skills/architect-design/SKILL.md), including the **2-Layer Abstraction Rule** and **Multi-File Orchestration Protocol**.
2. **Synchronize & Maintain Documentation**: Keep high-level blueprints in [architecture.md](architecture.md) (system context, Mermaid diagrams, public symbols) and low-level technical specifications in [references/architecture.md](references/architecture.md) (DB schemas, CSV layouts, state machines) strictly synchronized.
3. **Execute Orchestration Pipeline**: Plan the sequence of structural changes, apply updates across multiple modules bottom-up, and ensure all system components (code, tests, config, and docs) remain consistent.
4. **Architecture Sync Verification**: Run `python .agents/skills/architecture-sync/scripts/check_sync.py` to verify all public components are documented.
5. **Quality Verification**: Run `pytest tests/` and `ruff check .` to confirm structural and code integrity.
6. **Strict Boundaries & Format Requirement**: Adhere to [.agents/rules/workspace.md](.agents/rules/workspace.md). Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.