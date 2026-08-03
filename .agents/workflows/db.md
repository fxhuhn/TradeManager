---
description: "Run database schema changes, migration validation, and backup verification using the sqlite-persistence skill."
trigger: "/db"
---

# /db Command Workflow

When the user invokes `/db` or requests database schema changes, migration work, or persistence audits, you must:

1. **Activate the SQLite Persistence Skill**: Load and execute the instructions in [.agents/skills/sqlite-persistence/SKILL.md](.agents/skills/sqlite-persistence/SKILL.md).
2. **Enforce Database Invariants**: Verify WAL mode, foreign key cascades, Decimal storage, and the connection/transaction pattern as defined in the skill.
3. **Schema Migration**: If schema changes are required, update the migration scripts and verify via `schema_version` table.
4. **Architecture Sync**: Any modification to database schemas or helper functions in `app.core.db` must be updated in `architecture.md` and `references/architecture.md`.
5. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.
