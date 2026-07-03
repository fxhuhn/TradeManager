---
description: "Expert Python SDET & Testing Instructions."
trigger: "/test"
---

# /test Command Workflow

When the user invokes `/test` or requests to generate/execute unit or integration tests, you must:

1. **Activate the Tester Skill**: Load and execute the instructions in [.agent/skills/python-tester/SKILL.md](.agent/skills/python-tester/SKILL.md).
2. **Execute Destructive Testing**: Plan and write robust test cases targeting edge conditions, data poisoning, resource constraints, and time dependencies.
3. **Assert Code Correctness**: Use parameterization and mocks to test cleanly without network or disk dependency, maintaining strict financial precision checks.
