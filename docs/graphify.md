# Graphify Dev-Tool Operations

This document records the decision to adopt [Graphify](https://github.com/Graphify-Labs/graphify)
as a **development-time** knowledge-graph tool for codebase navigation (issue #116).

Graphify maps this repo (code + markdown) into a queryable graph with labeled
edges (`calls`, `imports`, `inherits`, `references`) and confidence tags
(`EXTRACTED` vs `INFERRED`). Code and markdown structure are parsed **locally
with tree-sitter AST — zero LLM, nothing leaves the machine**. Only docs/PDFs/
media would need a model backend, which we do **not** enable.

## Scope and invariants

- **Dev-time only.** Graphify never runs inside the Hermes container, never
  touches `/opt/data` or the vault, and never runs as root.
- **Not a gbrain/mnemosyne replacement.** gbrain covers vault notes; mnemosyne
  covers conversation memory. Graphify's scope is **codebase navigation only**.
- **This repo only.** We do not map `/opt/data` (private state repo) or client
  project repos.

## Decisions (issue #116 open questions)

| Question | Decision |
|---|---|
| Which repo(s) to map? | This repo only (pilot). |
| Commit `graphify-out/`? | Commit `graph.json` + `GRAPH_REPORT.md` + `graph.html` as the onboarding map (the HTML is the human-viewable interactive graph, openable on checkout with no setup). Gitignore `manifest.json`, `cache/`, `cost.json`, and the `.graphify_*` markers. |
| Enable MCP server? | **Deferred.** The `graphifyy[mcp]` extra is the least-stable surface (mcp 2.0 breakage history). Revisit only if cross-agent querying is needed. |
| Git hook auto-rebuild? | **Deferred.** Regenerate deliberately via `make graphify` (snapshot), not on every commit. Revisit after observing committed-snapshot behavior. |
| Hermes skill install? | **Rejected.** `graphify install --platform hermes` writes to `~/.hermes/skills/`, conflicting with the two-scope skill model (`skills-factory/` + `agent-state/skills/`) and the issue #69 guards. |

## Installation

Graphify lives in a dedicated, gitignored venv — **not** the pinned test venv
(`venv/`), which is guarded by the `check-test-env.py` drift hook.

```bash
python3 -m venv dev-tools-venv
dev-tools-venv/bin/pip install graphifyy==0.9.45
```

## Regeneration

```bash
make graphify        # = dev-tools-venv/bin/graphify update .
```

`graphify update .` re-extracts code (local AST) **and** markdown structure
(headings, doc↔doc links) with no LLM. It does **not** produce doc→code
semantic edges — that requires the LLM backend, which we keep disabled.

Regeneration is **operator-initiated and deliberate** (a snapshot), not
automatic. After regenerating, commit the updated `graph.json` +
`GRAPH_REPORT.md` so the map stays fresh for onboarding.

**Test suite excluded.** `tests/` is in `.graphifyignore`: the test suite
dominates the node count (70% of nodes) and drowns out the production-code
signal. The graph maps the actual code for navigation, not the test suite.
Graphify auto-backups the previous graph under `graphify-out/<date>/` on
prune; those backups are gitignored.

## Committed artifacts and PII guardrails

- **Committed:** `graphify-out/graph.json` (canonical machine-readable graph),
  `graphify-out/GRAPH_REPORT.md` (architecture summary), and
  `graphify-out/graph.html` (interactive human-viewable graph — open it in a
  browser on checkout; the onboarding map).
- **Gitignored:** `manifest.json` (per-file `mtime`/`seen` timestamps churn on
  every run and trip pii_guard as phone numbers),
  `cache/` (extraction cache), `cost.json` (local token tracking), and the
  `.graphify_*` markers.
- **pii_guard:** `GRAPH_REPORT.md` community "Cohesion score" floats can pass
  the Luhn credit-card check; a path-scoped allowlist entry in `.pii-allowlist`
  covers this generated metric. `graph.json` is clean.
- **gitleaks:** passes on the committed artifacts.

## Governance

- **Pin:** `graphifyy==0.9.45`. Graphify is pre-1.0 with daily releases; do not
  chase them. Re-pin only when a feature is needed or after 1.0.
- **Upgrade:** deliberate, reviewed upgrades. After upgrading, re-run
  `graphify update .` and re-verify pii_guard/gitleaks on the committed set.
- **Rollback:** remove `dev-tools-venv/`, remove `graphify-out/`, revert the
  `.gitignore`/`.pii-allowlist`/`Makefile`/`AGENTS.md` changes.
- **No CI integration.** Graphify stays out of `fast-tests.yml` (the
  self-hosted runner has no venv support; a daily-release tool is a
  maintenance tax with no runtime benefit).
- **Staleness:** the committed graph is a snapshot. Regenerate before relying
  on it after significant structural changes (new top-level dirs, moved
  modules, new scripts).

## Querying

```bash
dev-tools-venv/bin/graphify query "what connects auth to the database?"
dev-tools-venv/bin/graphify path "workspace_sync.py" "tasknotes_mcp_core.py" --undirected
dev-tools-venv/bin/graphify explain "tasknotes_lock_run.py"
```

`query`/`explain` operate on symbol-level nodes and are strongest for
cross-file relationship discovery (call graphs, import chains) — the case
where grep is imprecise.

## OpenCode integration

OpenCode interfaces with the graph in two ways:

- **Plugin** (`.opencode/plugins/graphify.js`, registered in the root
  `opencode.json`): a `tool.execute.before` hook that prepends a one-time
  reminder before bash calls when `graphify-out/graph.json` exists. It only
  *suggests* running `graphify query` — it does not execute graphify itself,
  has no telemetry, and degrades gracefully (no-op if the graph is absent).
  It coexists with `pii-commit-guard.mjs` (both are bash-only
  `tool.execute.before` hooks).
- **AGENTS.md section**: query-first guidance pointing at the committed graph.

Notes:
- The plugin is registered in the **root** `opencode.json`, not
  `.opencode/opencode.json` (which opencode would not read here).
- The venv is gitignored; if `dev-tools-venv/` is absent, the CLI is
  unavailable but the committed graph + plugin reminder still work.
