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

## Versioning

This project uses [Semantic Versioning](https://semver.org/) (SemVer) with the format `MAJOR.MINOR.PATCH`:

- **MAJOR**: Breaking changes (API changes, incompatible config changes)
- **MINOR**: New features (backwards compatible)
- **PATCH**: Bug fixes (backwards compatible)

### Version Files

| File | Component | Purpose |
|------|-----------|---------|
| `scripts/VERSION` | Pipeline scripts | Version for dvd-iso.sh, dvd-encoder.sh, dvd-transfer.sh |
| `web/VERSION` | Dashboard | Version for web UI (also update `DASHBOARD_VERSION` in dvd-dashboard.py) |

### When to Update Versions

| Change Type | Version Bump | Examples |
|-------------|--------------|----------|
| Bug fix | PATCH (0.0.X) | Fix encoding failure, correct state handling |
| New feature | MINOR (0.X.0) | Add status page, add preview generation |
| Breaking change | MAJOR (X.0.0) | Change config format, rename state files |

### Update Checklist

**For pipeline script changes:**
1. Update `scripts/VERSION`
2. Commit with the feature/fix

**For dashboard changes:**
1. Update `web/VERSION`
2. Update `DASHBOARD_VERSION` constant in `web/dvd-dashboard.py`
3. Commit with the feature/fix

**For changes affecting both:**
1. Update both VERSION files
2. Keep versions in sync if they share dependencies

### Example

Adding a new dashboard feature like the status page:
```bash
# Update version from 1.0.0 to 1.1.0
echo "1.1.0" > web/VERSION

# Also update the constant in dvd-dashboard.py
# DASHBOARD_VERSION = "1.1.0"
```

## Incremental Refactoring

We address technical debt incrementally alongside feature work. See [REFACTORING_GOALS.md](./REFACTORING_GOALS.md) for current goals.

### Workflow

1. **Before starting a feature**: Check if any refactoring goals relate to the code you'll touch
2. **During implementation**: Look for small refactoring opportunities that align with goals
3. **Scope appropriately**: Refactoring should be ~10-20% of feature effort, not a separate task
4. **After completion**: Update REFACTORING_GOALS.md if you made progress

### Example

If adding a new health metric and the goal is to extract helpers:
- Add the new metric function
- While there, move 1-2 related existing functions to a helper module
- Update imports in the main file
- Commit together as part of the feature

This keeps the codebase improving without dedicated refactoring sprints.

## Code Style

- Bash scripts: Use `set -euo pipefail`
- Functions: Use lowercase with underscores (`my_function`)
- Variables: Use UPPERCASE for config, lowercase for local
- Always quote variables: `"$var"` not `$var`
