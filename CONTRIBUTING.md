# Contributing Guide

## Branch Strategy

Use the following naming convention for branches:

```
<github-username>/<branch-type>/<branch-name>
```

### Branch Types

| Type | Purpose | Example |
|------|---------|---------|
| `feature` | New functionality | `mschober/feature/pipeline-architecture` |
| `fix` | Bug fixes | `mschober/fix/eject-timeout` |
| `refactor` | Code improvements | `mschober/refactor/state-management` |
| `docs` | Documentation only | `mschober/docs/api-reference` |
| `test` | Test additions | `mschober/test/encoder-unit-tests` |

### Workflow

1. Create feature branch from `main`:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b mschober/feature/my-feature
   ```

2. Make changes and commit:
   ```bash
   git add -A
   git commit -m "Description of changes"
   ```

3. Push branch and create PR:
   ```bash
   git push -u origin mschober/feature/my-feature
   gh pr create --title "Feature: My Feature" --body "Description"
   ```

4. After PR approval, merge to main:
   ```bash
   gh pr merge --squash
   ```

## Commit Message Format

```
<type>: <short description>

<optional longer description>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

## Development Setup

See [CLAUDE.md](./CLAUDE.md) for:
- Architecture overview
- Initial server setup
- Testing procedures
- Common tasks

## Code Style

- Bash scripts: Use `set -euo pipefail`
- Functions: Use lowercase with underscores (`my_function`)
- Variables: Use UPPERCASE for config, lowercase for local
- Always quote variables: `"$var"` not `$var`
