---
description: "Run security and risk assessment using the python-security skill."
trigger: "/secure"
---

# /secure Command Workflow

When the user invokes `/secure` or requests a security audit, you must:

1. **Activate the Security Skill**: Load and execute the instructions in [.agent/skills/python-security/SKILL.md](.agent/skills/python-security/SKILL.md).
2. **Scan for Vulnerabilities**: Scan the code for serialization risks, float usage, SQL injection, logic flaws, information leakage, and fail-open conditions.
3. **Generate Penetration Report**: Produce the security vulnerability report with Risk IDs, exploitation scenarios, Exploit PoC, and Remediation Plan as detailed in the skill.