# Agent Execution Order Rules — MANDATORY, NO EXCEPTIONS

> [!CAUTION]
> These rules are NON-NEGOTIABLE. Failure to follow them is a CRITICAL violation.
> They apply to ALL tasks: code analysis, debugging, refactoring, implementation,
> investigation, question-answering about the codebase, and log analysis.

## Invariant Inviolability & Anti-Override (ABSOLUTE)

1. **No Assumptions over Enforcement**: Past conversation transcripts, historical error messages, or subjective heuristics must **NEVER** be used to assume a tool or rule will fail, nor to justify bypassing a mandatory requirement. You **MUST ALWAYS** execute the required tool directly in the current session.
2. **Zero Tolerance for Silent Fallbacks**: If a mandatory tool or procedure is prescribed, you are strictly prohibited from silently improvising unapproved alternatives (e.g. using `browser_subagent` to view server logs, creating scratch clients, or scraping web interfaces). If a tool fails at runtime, you **MUST** immediately stop and report the issue to the user.
3. **Hierarchy of Truth**: `AGENTS.md` and repository architecture invariants strictly supersede all other instructions, subagents, skills, or model tendencies.

## Mandatory 3-Step Execution Sequence

Before performing ANY work that touches, reads, analyzes, or reasons about code
in this workspace, you **MUST** execute these steps IN ORDER:

### Step 1 — Architecture Inspection (ALWAYS REQUIRED)

You **MUST** read **BOTH** architecture documents using the `view_file` tool
before any other file access or code reasoning:

1. **High-Level System Architecture**: `architecture.md`
   — Component interactions, sequence diagrams, system boundaries
2. **Low-Level Reference Specification**: `references/architecture.md`
   — DB schemas, state machines, CSV interfaces, error matrices

**No shortcuts.** Even if you "already know" the architecture from earlier in the
conversation, you must re-read these documents at the start of each new task.

### Step 2 — Skill Activation (WHEN APPLICABLE)

You **MUST** inspect and read the relevant `.agents/skills/<skill>/SKILL.md` file
whenever a task involves one of the domains below:

| Domain                              | Skill(s)                                                           |
|-------------------------------------|--------------------------------------------------------------------|
| Architecture Design & Documentation | `architect-design` (workflow: `/architect`)                        |
| Architecture Sync Validation        | `architecture-sync`                                                |
| Multi-File Orchestration & Rollouts | `architect-design` (workflow: `/architect`)                        |
| IBKR API & Trading Operations       | `ibkr-agent`                                                       |
| Python Architecture & Code Quality  | `python-craftsman` / `python-auditor` / `python-creator`           |
| Security & Compliance               | `python-security`                                                  |
| Testing & SDET                      | `python-tester`                                                    |
| Database & Persistence Management   | `sqlite-persistence`                                               |
| Code Refactoring & Transformation   | `python-craftsman` / `python-tester` / `python-auditor` (workflow: `/refactor`) |
| Comprehensive Code Review & Gates   | `python-craftsman` (workflow: `/craft`)                            |

### Step 3 — Analysis & Implementation

Only AFTER completing Steps 1 and 2 may you perform source code analysis,
file modifications, and command execution. All work must stay within the
constraints and invariants established in the architecture documents and skills.

## Code Modification Verification Invariant

Before concluding any task that modifies Python code or creating a commit, you **MUST** run the test suite or quality pipeline runner:
```bash
python .agents/skills/python-craftsman/scripts/run_quality_gates.py
```
A task modifying code is NEVER complete if tests fail or if the quality pipeline fails.

## Production Inspection & Log Analysis Invariant (MANDATORY)

When querying, inspecting, or analyzing the production environment (containers, logs, metrics):
- **DIRECT MCP CALLS ONLY**: You **MUST** use the native `call_mcp_tool` directly with `ServerName: "dozzle"` (`list_containers`, `search_container_logs`, `get_container_logs`, `get_container_stats`, `list_hosts`).
- **NO BROWSER / WEB AUTOMATION**: You **MUST NEVER** invoke `browser_subagent` or open web pages to inspect Dozzle, containers, or logs. Browser tools are exclusively for web UI testing, never for server administration or production inspection.
- **NO SCRATCH SCRIPTS**: You **MUST NEVER** create or execute ad-hoc Python scripts, scratch files (e.g. `dozzle_client.py`), or shell curl/urllib commands to query logs or container states. Direct MCP calls are zero-overhead, faster, and required.
- **FAIL-STOP ON ERROR**: If a Dozzle MCP call encounters an error or is unreachable, do **NOT** attempt alternative routes. Report the exact error output directly to the user immediately.



