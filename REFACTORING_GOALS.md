# Refactoring Goals

This document tracks technical debt and refactoring opportunities. When working on a feature that touches related code, consider incorporating small refactoring efforts.

## Active Goals

### 1. Break apart `web/dvd-dashboard.py`

**Priority**: Medium
**Effort**: Large (incremental)
**Status**: Not started

The dashboard file has grown to ~4000+ lines and contains:
- HTML templates (multiple large template strings)
- Route handlers
- Helper functions for system monitoring
- API endpoints
- Cluster management functions

**Target structure**:
```
web/
├── dvd-dashboard.py          # Main Flask app, routes, minimal code
├── templates/
│   ├── base.html             # Shared layout, nav, footer
│   ├── dashboard.html        # Main dashboard
│   ├── health.html           # Health monitoring page
│   ├── cluster.html          # Cluster status page
│   ├── config.html           # Configuration editor
│   ├── status.html           # Service status page
│   ├── identify.html         # Pending identification
│   └── architecture.html     # Architecture docs
├── api/
│   ├── __init__.py
│   ├── health.py             # /api/health, /api/processes, /api/kill
│   ├── cluster.py            # /api/cluster/*, /api/worker/*
│   ├── config.py             # /api/config, /api/config/save
│   └── pipeline.py           # /api/status, /api/trigger, /api/queue
└── helpers/
    ├── __init__.py
    ├── system.py             # CPU, memory, load, temps, I/O
    ├── pipeline.py           # Queue items, state files, locks
    └── cluster.py            # Peer status, capacity, distribution
```

**Incremental approach**:
1. Extract helper functions first (lowest risk)
2. Move API endpoints to blueprints
3. Convert template strings to Jinja2 files
4. Refactor routes to use blueprints

**When touching this file**:
- If adding a new helper function → consider placing in appropriate `helpers/` module
- If adding a new API endpoint → consider using Flask blueprints
- If modifying a template → consider if it can be extracted to a file

---

### 2. Add unit tests for web dashboard

**Priority**: Medium
**Effort**: Medium (incremental)
**Status**: In progress (Phase 1 complete)

The dashboard has no automated tests. Helper functions and API endpoints should be testable.

**See [TESTING.md](./TESTING.md) for the full testing roadmap with phases, fixtures, and examples.**

**Progress**:
- ✅ Phase 1: Pure functions (no mocking) - `tests/test_pure_functions.py`
- ⬜ Phase 2: System helpers (file I/O mocking)
- ⬜ Phase 3: Pipeline helpers (state file mocking)
- ⬜ Phase 4: Subprocess functions
- ⬜ Phase 5: Cluster helpers
- ⬜ Phase 6: API endpoints (Flask test client)

**Test structure**:
```
tests/
├── __init__.py
├── conftest.py               # Shared fixtures ✅
├── test_pure_functions.py    # Pure logic tests ✅
├── test_system_helpers.py    # CPU, memory, load, temps, I/O
├── test_pipeline_helpers.py  # Queue, state files, locks
├── test_cluster_helpers.py   # Peer status, capacity
└── test_api_endpoints.py     # Flask test client for API routes
```

**When touching dashboard code**:
- If adding a new helper function → consider writing a test for it
- If fixing a bug → add a regression test
- If refactoring → ensure existing behavior is preserved with tests

---

## Completed Goals

(None yet)

---

## How to Use This Document

1. **Before starting a feature**: Check if any refactoring goals relate to the code you'll touch
2. **During implementation**: Look for small refactoring opportunities that align with goals
3. **After completion**: Update this document if you made progress on a goal
4. **Adding goals**: When you notice technical debt, add it here with priority and effort estimates
