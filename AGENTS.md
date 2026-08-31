# AGENTS.md

Purpose: root guidance for AI assistants working with Josemar Assistente. Keep this file focused on repository-wide rules and routing. Detailed subsystem guidance belongs in the narrowest applicable `AGENTS.md`, skill reference, or `docs/` runbook.

## Prompt Language Policy

- Author all LLM-facing prompt and instruction files in English, even when the assistant is expected to interact with users in another language.
- This applies to `AGENTS.md`, `SKILL.md`, playbooks, starter-state templates, harness instructions, and prompt examples committed to this repo.
- Runtime interactions may still follow the user's preferred language; keep source prompt files language-neutral by writing them in English.

## Repository Overview

Josemar Assistente is a self-hosted AI assistant built on Hermes, running in Docker with Telegram integration.

Core architecture:
- Hermes gateway runtime (dashboard/API/Telegram/cron/skills)
- two-scope skills (repo-owned `skills-factory/` + user-owned `agent-state/skills/`)
- Git-backed state sync (`scripts/workspace_sync.py`)
- Obsidian vault operations through native gbrain plus the bounded TaskNotes MCP exception
- encrypted vault recovery
- optional Mnemosyne semantic conversation memory
- optional browser-control reverse-tunnel overlay
- optional aux-ml queue service

Important top-level paths:
- `agent-state/`: nested private repo for user-owned state
- `docs/`: architecture and operations documentation; start with `docs/README.md`
- `scripts/`: runtime, sync, backup, privacy, and helper scripts
- `skills-factory/`: repo-owned core skills
- `tests/`: unit, contract, and gated runtime tests
- `.github/workflows/`: CI/CD automation; obey `.github/workflows/AGENTS.md`

## Repository-First Work

- Read the applicable `AGENTS.md` files before editing a subtree.
- Inspect source, config, tests, and relevant durable documentation before changing behavior.
- Treat `graphify-out/` as a navigation aid only; verify conclusions against source. See `docs/graphify.md`.
- Create feature branches for non-trivial work. Do not commit directly to `main` unless explicitly requested.
- Keep commits focused and scoped.

## Documentation Architecture and Maintenance

The canonical documentation architecture and update rules are in `docs/documentation-policy.md`. Read it when changing documentation or when code/config/test changes affect documented behavior.

Repository-wide rules:

1. **Executable truth first.** Source, config, schemas, tests, and generated/runtime checks define actual behavior. Documentation explains those contracts; do not make documentation the only implementation of a behavior.
2. **Use the narrowest reliable scope.** Root guidance contains universal rules. Nested `AGENTS.md` files contain subtree-specific constraints and must route workers to narrower canonical docs when a class of change makes those docs relevant.
3. **Parent guidance owns discovery.** A worker must not need to know a nested catalog/runbook exists before touching the behavior it documents. Parent guidance must identify which change classes require consulting or updating narrower docs.
4. **Routine skill use stays self-contained.** A main `SKILL.md` must contain everything needed for normal day-to-day operations. Move uncommon schemas, compatibility matrices, recovery/upgrade procedures, and other deep material to `references/` and link it explicitly. Context-size targets are heuristics, not hard limits.
5. **A behavior change and its durable documentation are one change.** Before completion, classify whether the change affects harness instructions, runtime-agent behavior, operator procedure, configuration, contributor/test procedure, onboarding, or templates. Update every affected canonical document and deliberately duplicated safety summary in the same PR. If no docs change is required, record why in the implementation report.
6. **Do not use issue/PR discussion as the only shipped documentation.** Stable behavior belongs in repository documentation.
7. **Distinguish defaults from runtime state.** Use the vocabulary defined in `docs/documentation-policy.md`: repository default, supported mode, operator-enabled state, current runtime state. Verify mutable current state mechanically when it matters rather than freezing it into static prose.

Safety and operational invariants may be repeated intentionally so they are visible at the point of use. When changing a duplicated invariant across `AGENTS.md`, skills, runbooks, workflows, or contract tests, inspect and update every applicable copy in the same change.

## Pinned Dependency Upgrades

Hermes, gbrain, Mnemosyne, Bun, container images, and selected helper tools are pinned, and Josemar carries local compatibility patches around some upstream components. Treat upgrades as compatibility changes, not isolated version bumps: verify the upstream release/ref, re-check local patches and runtime/config contracts, inspect affected runbooks/tests, and run the relevant focused and Docker/Compose validation before considering the upgrade complete.

## Agent State Repo Rules

`agent-state/` is a nested private repo and source of truth for user-owned state.

- Personality/context files live there using Hermes-native paths (`SOUL.md`, `memories/MEMORY.md`, `memories/USER.md`, `AGENTS.md`, etc.).
- User-owned skills live there under `agent-state/skills/*`.
- Only paths in `.sync-manifest` are auto-versioned by sync.
- When modifying user state, commit/push inside the `agent-state` repo when requested.

Never version the full Hermes `config.yaml`; it mixes operational, security, and deployment controls and remains repo/operator/runtime-owned and unversioned. State-owned provider/model selection is the sparse `hermes/models.yaml` overlay only.

## Skills Ownership and Boundaries

- Repo-owned skills: `skills-factory/*` -> copied to `/opt/josemar/skills`.
- User-owned skills: `agent-state/skills/*` -> synced into `/opt/data/skills`.
- Native gbrain is the canonical general vault interface. Agent-facing general vault access uses the public `gbrain` command, which provides the safe adapter behavior.
- The bounded `tasknotes` MCP is the only specialized task-write exception. It uses short-lived private native gbrain commands internally under the shared lock; it must never route through the public `gbrain` wrapper. Its direct vault-file write is limited to the documented derived Daily Note task-link projection. See `docs/tasknotes-mcp.md`.
- Internal private native gbrain paths are operator/implementation interfaces, never agent-facing commands. See `docs/gbrain-operations.md` and `skills-factory/gbrain/SKILL.md`.
- Automatic skill creation/curation remains disabled until the repository's documented re-enable criteria are satisfied. Intentional user-skill authoring must be explicit and stay under `agent-state/skills/*`.

## gbrain Safe-Access Non-Negotiables

These invariants apply to every assistant, cron, skill, and operator path that touches gbrain state. Detailed operation/recovery procedures live in `docs/gbrain-operations.md`; TaskNotes specifics live in `docs/tasknotes-mcp.md`.

1. **No root execution.** Run gbrain/vault Git operations as the Hermes runtime user. Runtime gbrain state under `/opt/data/.gbrain` belongs to that user.
2. **Public `gbrain` is the agent-facing path.** Chat, skills, and external general vault actions use the public `gbrain` command. Do not present `/opt/josemar/libexec/gbrain-native` or other private native paths as agent commands.
3. **No concurrent PGLite opens.** The gbrain database is single-writer PGLite.
4. **Cooperative lock.** All Josemar-owned gbrain-touching paths cooperate on `/opt/data/.locks/tasknotes.lock`.
5. **Maintenance windows must quiesce every documented owned writer/exporter before destructive recovery, reindex/rebuild, migration, or vault swap.** Follow the exact current checklist in `docs/gbrain-operations.md` / `docs/vault-recovery-operations.md`; do not rely on a stale copied job list in this root file.
6. **TaskNotes never nests the public wrapper.** Task mutations use the `task_*` MCP tools; TaskNotes retains its transaction lock and private native invocation and exposes no generic note-write tool.

## Skill Organization

Use `SKILL.md` for the routine path and `references/<topic>.md` for non-routine depth.

`SKILL.md` should contain the skill purpose, critical invariants, common operations, common reference paths, and enough guidance to complete normal requests without another file load. A frequently used skill may legitimately be longer than a rarely used skill. Rough line/section-size guidance is a review signal only; do not move common-path instructions out merely to satisfy a size target.

Move uncommon schemas/taxonomies, exhaustive command-output descriptions, compatibility matrices, recovery/upgrade procedures, and rare edge cases into `references/`. The main skill must explicitly tell the agent when to load each reference.

Good examples include `skills-factory/backup-operations/` and `skills-factory/tasknotes/`.

## Security Rules

1. Never commit secrets, credentials, tokens, private user state, PII, or private host/network details.
2. Keep credentials under `credentials/<service>/` and out of version control.
3. Keep `agent-state` private and respect `.sync-manifest` boundaries.
4. Public GitHub artifacts must describe system behavior impersonally and must not include user-specific private-state contents or anecdotes.
5. Run the repository's staged privacy/PII checks where applicable before commit.

## Testing and Validation

- New and changed behavior must include new or updated tests where practical. If a change is not practically testable, record the limitation.
- Prefer named Make targets because they encode supported environments and gates.
- Use `tests/README.md` to select focused and Docker/runtime-gated validation; do not load/run every expensive suite indiscriminately.
- For direct local Python tests, prefer `venv/bin/python3` when the repo virtualenv exists; CI/venv-less environments may use system Python only when dependencies are supplied as documented.
- Run `make test` and `make verify` as separate top-level commands with deliberate timeouts of at least 30 minutes. Intentionally expensive Docker/runtime gates may need roughly 45–60 minutes when repository evidence supports that expectation.
- A timeout is incomplete validation, not success or failure unless independent conclusive failure output was produced. Do not rerun an identical command solely because an unnecessarily short default timeout expired.

Common commands:

```bash
make test
make verify
venv/bin/python3 -m unittest tests.gbrain.test_gbrain_wrapper_contract -v
```

## Key Routing References

- `docs/README.md` — documentation map, audience, and load/update routing
- `docs/documentation-policy.md` — canonical documentation architecture and maintenance contract
- `README.md` — top-level orientation and operator onboarding
- `tests/README.md` — supported validation commands and test-suite routing
- `.github/workflows/AGENTS.md` — workflow-specific change constraints and documentation dependencies
- `docs/gbrain-operations.md` — gbrain operator architecture and operations
- `docs/tasknotes-mcp.md` — TaskNotes MCP contracts and operations
- `docs/vault-recovery-operations.md` — vault-recovery architecture/recovery
- `docs/obsidian-operations.md` — Obsidian sync/backup operations
- `docs/browser-control.md` — browser-control architecture/operations
- `docs/mnemosyne-operations.md` — Mnemosyne operations
- `docs/aux-ml.md` — aux-ml operations
- `docs/graphify.md` — Graphify navigation/freshness/governance
