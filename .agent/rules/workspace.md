# Strict Workspace Boundary Protocol

- You are strictly locked into the current working directory from which you were invoked.
- Never assume, hardcode, or switch to paths belonging to other repositories (e.g., do not mix TradeManager and croc-trader_2).
- All file operations (read, write, list) and shell commands MUST use repository-relative paths starting from the current workspace root.
- Dynamically detect the current repository name using `git rev-parse --show-toplevel` if context confusion occurs.
