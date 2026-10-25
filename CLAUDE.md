@AGENTS.md

## Claude Code mapping for AGENTS.md
- Teammates live in `.agents/agents/*.md` (symlinked as `.claude/agents`) and are invoked with the `Agent` tool (`subagent_type: researcher|architect|coder|reviewer|janitor`).
- Model tiers: `flash_lite`→`haiku`, `flash`→`sonnet`, `pro`→`opus`, `inherit`→`inherit` (pass as the `model` parameter).
- `Workspace: "branch"` → `isolation: "worktree"` (needs a git repo).
- Full mapping and tier notes: `.agents/memory/orchestrator.md`.
