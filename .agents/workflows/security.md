---
description: "Run security and risk assessment using the python-security skill."
trigger: "/security"
---

# /secure Command Workflow

When the user invokes `/security` or requests a security audit, you must:

1. **Activate the Security Skill**: Load and execute the instructions in [.agents/skills/python-security/SKILL.md](.agents/skills/python-security/SKILL.md).
2. **Scan for Vulnerabilities**: Scan the code for serialization risks, float usage, SQL injection, logic flaws, information leakage, and fail-open conditions.
3. **Generate Penetration Report**: Produce the security vulnerability report with Risk IDs, exploitation scenarios, Exploit PoC, and Remediation Plan as detailed in the skill.
4. **Format Requirement**: Return only repository-relative paths, direct code diffs, or structured markdown tables. No generic text summaries.