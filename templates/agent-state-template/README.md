# Josemar Agent State Template

Template for the [Josemar Assistente](../) agent state repository.

This is a **private** git repo that stores the agent's identity, personality, user-owned skills, cron jobs, and concise memory files. At runtime it is checked out directly at Hermes home (`/opt/data`) so tracked files follow Hermes' native structure.

LLM-facing prompt and instruction files in this template should be authored in English, even when the assistant is expected to interact with the user in another language.

## Setup

1. Create a new **private** GitHub repository
2. Do NOT initialize with README (avoids merge conflicts)
3. Clone this template and push:
   ```bash
   git clone <this-template-url> my-agent-state
   cd my-agent-state
   rm -rf .git
   git init
   git add -A
   git commit -m "Initialize agent state from template"
   git branch -M main
   git remote add origin <your-private-repo-url>
   git push -u origin main
   ```
4. Set the repo URL in your josemar-assistente deployment:
   - **Environment variable:** `WORKSPACE_STATE_REPO=https://github.com/user/josemar-agent-state.git`
   - **GitHub secret:** `WORKSPACE_REPO_TOKEN` (GitHub PAT with `repo` scope and **write** permissions)

## First-Time Bootstrap

On the first run, if the state repo has no personality files (`SOUL.md`, `memories/USER.md`, `AGENTS.md`), Hermes will guide initial setup through normal agent interaction.

**For clean bootstrap behavior, the initial commit to the state repo should NOT include personality `.md` files.** Only include:
- `.gitignore`
- `.sync-manifest`
- `skills/`
- `cron/jobs.json`
- `avatars/`
- `hermes/` (`models.yaml` + `skill-toggles/` sidecars — shipped template state, not personality; the `command-allowlist/` family ships no template files and appears only after the first permanent command approval is saved)

Personality and memory files (`SOUL.md`, `memories/USER.md`, `AGENTS.md`, optionally `memories/MEMORY.md`) are created/maintained by Hermes and automatically versioned by periodic sync.

## Skill Ownership Model

This project separates skills by ownership:

- **Core repo-owned skills** ship from the main repository (`skills-factory/`) and are bundled into the Docker image.
- **User-owned skills** live in this private state repo (`skills/`) and are specific to each user/deployment.

Do not copy user-specific skills into the main repository. Keep them in the state repo.

### Skill edit policy

- Treat repo-owned core skills (`/opt/josemar/skills/*`) as maintained through normal development in the main public repository (branch/commit/PR).
- In runtime self-improvement flows, prefer writing a patch proposal for repo-owned skills instead of creating sidecar skills (for example `*-pitfalls`).
- User-owned skills in `/opt/data/skills/*` can be patched directly and are expected to be versioned through the state repo sync flow.
- Runtime-created user skills are auto-registered for sync when their directory contains `SKILL.md`.
- Avoid duplicate skill sprawl: patch an existing user-owned skill before creating a new skill with overlapping scope.

**To trigger bootstrap-like setup on an existing deployment:**
1. Delete all personality `.md` files from the state repo and push
2. Deploy with `fresh_start: true` (deletes Docker volume, forces fresh clone)
3. On first message, Hermes will rebuild baseline context from your prompts and state

## File Map

| File | Purpose | Created by |
|------|---------|------------|
| `AGENTS.md` | Operating instructions for the agent | Agent / manual |
| `SOUL.md` | Persona, tone, boundaries | Agent / manual |
| `memories/USER.md` | User information and preferences | Agent / manual |
| `memories/MEMORY.md` | Long-term curated memory | Agent |
| `BOOT.md` | Startup checklist (optional) | Template / manual |
| `skills/` | Agent skills (SKILL.md + executables) | Agent / manual |
| `cron/jobs.json` | Cron job definitions loaded by Hermes | Manual / agent |
| `avatars/` | Agent avatar images | Manual |
| `hermes/models.yaml` | State-owned Hermes model selections (strict selection-only v1: default, fallback, 11 allowlisted auxiliary tasks, cron defaults) | Manual |
| `hermes/command-allowlist/` | State-owned Hermes runtime command allowlist sidecars (strict v1: `default.json` for the workspace root, `profiles/<canonical>.json` per named profile) | Runtime (permanent approval save) / manual |

## Mnemosyne Pilot: Archive Status of Memory Files

When the optional Mnemosyne pilot overlay (`docker-compose.mnemosyne.yml`) is
enabled, the assistant uses upstream-native Mnemosyne semantic memory instead
of static file injection. In that mode:

- `memories/MEMORY.md` and `memories/USER.md` are **archived but not injected**.
  They remain at their versioned paths (tracked by `.sync-manifest` and
  `.gitignore`) as explicit **rollback material**. No automatic migration or
  deletion is performed; the files are left untouched on disk.
- The runtime Mnemosyne database lives separately under
  `/opt/data/mnemosyne/data` (a runtime SQLite store, NOT versioned here) and
  is preserved across rollback.
- To roll back to static injection, disable the Mnemosyne overlay (remove
  `docker-compose.mnemosyne.yml` from `COMPOSE_FILE`). The container init
  restores `memory.memory_enabled`/`user_profile_enabled` to true and removes
  only the installer-owned plugin/override-skill artifacts, while preserving
  the Mnemosyne DB for future re-activation.

The pilot is **passive-only ingestion**: automatic ingestion is passive raw
user-turn capture (global cross-session). Explicit upstream-native
mutation/management tools (including mutating operations) remain available to
the agent. No auto-sleep, reflection, or LLM consolidation runs in the pilot.
Full native Mnemosyne tools are available; this is upstream-native behavior.
LLM consolidation can infer summaries/facts but is intentionally off pending a
later explicit user decision on LLM provider, privacy, and cost. That decision
and the future option are documented in `docs/memory-embeddings-evaluation.md`.

## State-Owned Model Selections

`hermes/models.yaml` is the canonical state file for the agent's model
selections. It is a single root-only configuration — no profiles or
multiplexing.

- **Strict selection-only v1 contract.** ONLY `provider`/`model` selection is
  allowed. The file carries exactly:
  - `model.{provider, default}` — default model for primary agent turns
  - `fallback_providers[].{provider, model}` — ordered fallback list
  - `auxiliary.<task>.{provider, model}` — per-task model routing for exactly
    the 11 allowlisted auxiliary tasks (upstream dashboard order): `vision`,
    `web_extract`, `compression`, `skills_hub`, `approval`, `mcp`,
    `title_generation`, `triage_specifier`, `kanban_decomposer`,
    `profile_describer`, `curator`
  - `cron.{model, model_provider}` — fleet cron defaults (blank = inherit default)
  Individual cron job overrides stay in `cron/jobs.json` (per-job
  `model`/`provider` fields) and are NOT duplicated here.
- **Auxiliary auto rule.** `provider` is required and non-empty. When
  `provider` is exactly `auto`, `model` must be exactly `""` (upstream
  selects the model); every other provider requires a non-empty `model`.
  This rule applies only to auxiliary entries — root/fallback/cron keep
  their own semantics.
- **Sparse overlay; no auto-migration.** Only explicitly present entries
  overlay the runtime config; absent entries never clear runtime keys.
  Existing sparse v1 files that carry only a subset of the auxiliary slots
  remain valid and are never auto-mutated or auto-expanded. Adopting new
  slots is a manual edit: copy the desired entries from this template.
- **Forbidden in this file.** `base_url`, `api_mode`, `extra_body`, timeouts,
  token/context limits, `fallback_chain`, credentials/secret keys, provider
  definitions, endpoints, security/deployment topology, or any other Hermes
  config. The full Hermes `config.yaml` was reassessed and remains
  repo/operator/runtime-owned and unversioned — it mixes operational,
  security, and deployment controls. This file is versioned state for
  provider/model selection only.
- **Validation.** State changes are validated before sync commit; invalid
  files (unknown keys, forbidden fields, or schema violations) are rejected
  and never reach the runtime config.
- **Source of truth.** This file is the source of truth for model selections.
  Dashboard model changes are NOT source of truth — they live only in the
  untracked runtime `config.yaml` and are overwritten on the next
  sync/restart. To change the model durably, edit this file, commit, and let
  sync/restart apply it.
- **Persistence timing.** State changes are applied at sync/start. The
  workspace sync mirrors `hermes/models.yaml` to `/opt/data/hermes/models.yaml`
  and the container init applies it to the runtime config on startup.
- **Rollback.** Delete or revert this file, then sync/restart. The runtime
  config restores the repo model defaults from `config/hermes-config.yaml`
  on the next start.

## State-Owned Command Allowlist Sidecars

`hermes/command-allowlist/` holds the state-owned runtime command
allowlist. It follows the same profile layout as the skill toggles:

- **Ownership and paths.** Exactly `hermes/command-allowlist/default.json`
  mirrors the workspace root (base `HERMES_HOME`) and
  `hermes/command-allowlist/profiles/<canonical>.json` mirrors one named
  profile. Nothing else under `hermes/` is versioned: the deny-by-default
  `.gitignore` un-ignores only these exact shapes, and `.sync-manifest`
  carries exactly `hermes/command-allowlist/default.json` plus the
  sanctioned `hermes/command-allowlist/profiles/*.json` wildcard. Broader
  manifest globs are rejected by workspace-sync; the full Hermes
  `config.yaml` remains repo/operator/runtime-owned and is never versioned
  here or anywhere in this repo.
- **Strict v1 schema.** Each sidecar is one canonical JSON line, exactly
  `{"version": 1, "command_allowlist": ["..."]}` — both keys required,
  sorted/deduped non-empty strings. Wrong version, unknown keys, wrong
  types, missing keys, and empty/non-string entries are rejected.
- **Presence semantics.** Presence is authoritative: an explicit `[]`
  keeps the runtime ROOT-LEVEL `command_allowlist` key durably empty,
  while an ABSENT sidecar removes that key. An empty sidecar file is
  malformed, not an implicit clear.
- **Profile isolation.** Each sidecar applies only to its own
  `HERMES_HOME`; the default sidecar never affects named profiles and
  vice versa.
- **Permanent-write flows.** Dashboard/permanent command approvals go
  through the stateful runtime helper: the sidecar is written first and
  the runtime config second under one advisory lock, so a failed state
  write fails the save instead of silently diverging. Saving an explicit
  empty list is the durable way to keep the key empty; clearing the
  approval removes the sidecar and the runtime key.
- **Periodic sync validation.** Workspace sync validates every present
  sidecar with the canonical helper before staging, validates the
  committed sidecars before every push, and validates the remote
  candidates before any merge/acceptance. Profile sidecars must use the
  canonical profile filename contract: the manifest wildcard enumerates
  the family but never authorizes noncanonical filenames — they are
  rejected value-free (content never read or echoed) at every ingress.
  Invalid or malformed sidecar state fails the sync closed (nonzero,
  nothing staged or merged). A validator that is unavailable while
  sidecars are present also fails closed. Absence and deletion are
  always valid.
- **Migration.** On startup (before the repo template overwrites the
  runtime config) a NON-EMPTY runtime `command_allowlist` is extracted
  into an absent sidecar. Migration never overwrites an existing sidecar
  and never invents an explicit-empty one.
- **Deletion / unset rollback.** Delete a sidecar (or clear the approval
  in the dashboard) and sync: the absent sidecar removes the
  corresponding runtime key on the next apply/reconcile. To roll back the
  whole family, delete the sidecars, sync, and restart.
- **Privacy.** Never store secrets, tokens, passwords, or
  credential-bearing command literals in a sidecar: entries are plain
  command allowlist patterns versioned in git (the repo is private, but
  the files are still versioned history). Keep entries minimal and free
  of anything sensitive.
- **Validation errors.** Sync/validation errors identify the sidecar path
  and the structural violation only — allowlist contents never appear in
  errors, statuses, or logs.

## Security

- This must be a **private** repository
- `.gitignore` is deny-by-default, so only explicit state paths can be staged normally
- `.sync-manifest` explicitly lists what gets synced; the only wildcard
  entries are the three intentional template families (`avatars/*`,
  `hermes/skill-toggles/profiles/*.json`, and
  `hermes/command-allowlist/profiles/*.json`) — every broader form is rejected
- Never store API keys, tokens, or passwords here

## Sync Strategy

- On container start: agent's local changes are committed, then merged with remote (remote wins conflicts)
- Periodic: changes are auto-committed and pushed (configurable interval)
