# Graph Report - josemar-assistente  (2026-08-18)

## Corpus Check
- 90 files · ~181,226 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1802 nodes · 3641 edges · 98 communities (83 shown, 15 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 60 edges (avg confidence: 0.51)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `04ae752e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- workspace_sync.py
- vault_recovery_core.py
- build_cli
- mnemosyne_backup_core.py
- service.py
- Graphify Dev-Tool Operations
- vault_recovery_restore_core.py
- graphify.js
- _date_valued_frontmatter_keys
- _denormalize_bare_date
- JobScheduler
- tasknotes_mcp_core.py
- .update
- josemar-backup-status.py
- run_subprocess
- runner.py
- josemar-browser-control
- Mnemosyne Encrypted Backup Operations (Phase 3)
- Any
- mnemosyne_retrieval_eval/__init__.py
- check_versions
- Obsidian Operations Runbook
- main.py
- EvalRuntime
- vault-recovery-uploader.sh
- tasknotes_mcp.py
- Path
- LlamaRouterClient
- GBrain Operations Runbook
- josemar-gbrain
- schema.py
- transcribe_granite.py
- Remote Browser Control
- Josemar Assistente
- mnemosyne-backup-uploader.sh
- docker-hermes-init.sh
- Vault Recovery Operations (Phase 1 + Phase 2 + Phase 3)
- Any
- Actions
- Memory & Embeddings Evaluation (Issues #86 / #65)
- vault-recovery-recover.sh
- aux-ml
- ModelRegistry
- TaskNotes MCP Operations
- josemar_skill_state.py
- tasknotes_lock_run.py
- report.py
- Browser Control First-Time Setup
- Available Actions
- AuxMLService
- Mnemosyne Portuguese Retrieval Quality Gate (Phase 3)
- AGENTS.md
- Auxiliary ML Service (`aux-ml`)
- pii_guard.py
- save_disabled_skills_stateful
- run_ocr_task
- install-launcher.sh
- hermes-gbrain-embedding-refresh.py
- normalize_state
- Life Chronicle — Full Reference
- GitHub Workflows Documentation
- apply_models_overlay
- mnemosyne-backup-recover.sh
- Actions
- TaskNotes
- main
- mnemosyne-backup-restore.sh
- Josemar Agent State Template
- vault-recovery-restore.sh
- generate_slug
- vendor_faquad_ir.py
- Status Observation Reference
- ._verify_readback
- Recovery Checklist Reference
- Custom User Fields
- BOOT.md
- hermes-vault-recovery-export-cron.sh
- Browser Control Skill
- browser-tunnel/entrypoint.sh
- hermes-gbrain-embedding-refresh-cron.sh
- Backup Operations Skill
- Josemar User Schema Pack
- Laptop launcher support boundaries
- plugin
- aux-ml/entrypoint.sh
- pii-commit-guard.mjs
- PII Commit Check
- replace_once
- replace_once
- rclone-active-config.sh
- hermes-gbrain-refresh-cron.sh
- hermes-workspace-sync-cron.sh
- mnemosyne-backup-export.sh
- setup-pre-commit.sh
- vault-recovery-export.sh
- workspace-sync

## God Nodes (most connected - your core abstractions)
1. `AuxMLService` - 34 edges
2. `TaskNotesProfile` - 31 edges
3. `JobScheduler` - 27 edges
4. `TaskNotesEngine` - 27 edges
5. `_git()` - 27 edges
6. `export_generation()` - 26 edges
7. `LlamaRouterClient` - 24 edges
8. `ValidationError` - 24 edges
9. `CoreError` - 22 edges
10. `export_backup()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `AuxMLService` --uses--> `QueueFullError`  [INFERRED]
  aux-ml/app/service.py → aux-ml/app/jobs.py
- `AuxMLService` --uses--> `JobScheduler`  [INFERRED]
  aux-ml/app/service.py → aux-ml/app/jobs.py
- `lifespan()` --uses--> `LlamaRouterClient`  [INFERRED]
  aux-ml/app/main.py → aux-ml/app/llama_router.py
- `AuxMLService` --uses--> `LlamaRouterClient`  [INFERRED]
  aux-ml/app/service.py → aux-ml/app/llama_router.py
- `_validate_memory_policy()` --uses--> `ModelRegistry`  [INFERRED]
  aux-ml/app/main.py → aux-ml/app/model_registry.py

## Import Cycles
- None detected.

## Communities (98 total, 15 thin omitted)

### Community 0 - "workspace_sync.py"
Cohesion: 0.05
Nodes (97): _append_manifest(), _assert_remote_tree_safe(), _authenticated_fetch(), _checked_push(), _cleanup_git_env(), _commit_changes(), _configure_git(), _do_commit() (+89 more)

### Community 1 - "vault_recovery_core.py"
Cohesion: 0.05
Nodes (90): _active_schema_pack(), ActiveIndicatorError, _build_generation(), build_parser(), _canonical_env(), _cmd_export(), _cmd_latest(), _cmd_list() (+82 more)

### Community 2 - "build_cli"
Cohesion: 0.16
Nodes (11): apply_all_sidecars_and_policy(), build_cli(), _cli_apply_all(), _cli_sync_and_apply(), main(), ArgumentParser, Apply every present sidecar + enforce policy for every profile config. Public…, Run ``sync_command`` and apply sidecars/policy under one advisory lock.… (+3 more)

### Community 3 - "mnemosyne_backup_core.py"
Cohesion: 0.06
Nodes (67): Connection, _add_common_args(), build_parser(), _cmd_export(), _cmd_inspect_dr(), _cmd_install_restore(), _cmd_latest(), _cmd_list() (+59 more)

### Community 4 - "service.py"
Cohesion: 0.29
Nodes (10): _env_bool(), _env_float(), _env_int(), load_settings(), _parse_allowed_input_dirs(), Path, ValueError, Raised when settings values are internally inconsistent. (+2 more)

### Community 5 - "Graphify Dev-Tool Operations"
Cohesion: 0.20
Nodes (9): Committed artifacts and PII guardrails, Decisions (issue #116 open questions), Governance, Graphify Dev-Tool Operations, Installation, OpenCode integration, Querying, Regeneration (+1 more)

### Community 6 - "vault_recovery_restore_core.py"
Cohesion: 0.08
Nodes (69): _acquire_install_lock(), _active_schema_pack(), _append_step(), build_parser(), _cmd_install(), _cmd_rollback(), _cmd_verify(), _contain_absolute_paths() (+61 more)

### Community 11 - "JobScheduler"
Cohesion: 0.10
Nodes (6): JobRecord, JobScheduler, Atomically remove a job from the FIFO and transition it to ``cancelled``.…, Mark a job failed while it was in ``cancelling`` (model unload failed). This is…, Unified FIFO queue + job state store with atomic CAS transitions. A single…, _utc_now_iso()

### Community 16 - "tasknotes_mcp_core.py"
Cohesion: 0.06
Nodes (66): _build_gbrain_env(), check_git_state(), compute_profile_hash(), CoreError, _git_state_ok(), GitError, list_dir_no_follow(), list_tasks() (+58 more)

### Community 17 - ".update"
Cohesion: 0.10
Nodes (37): decode_page(), gbrain_capture(), gbrain_get_page(), Lock, MutationResult, Exclusive ``fcntl.flock`` with a bounded wait. The lock file lives at a…, Call ``gbrain call --source <id> get_page <json>`` and return parsed JSON. On a…, Call ``gbrain capture --stdin --slug <slug> --source <id> --json`` with… (+29 more)

### Community 23 - "josemar-backup-status.py"
Cohesion: 0.09
Nodes (45): check_identity(), _emit(), _empty_result(), _failure(), is_valid_generation_id(), main(), _manifest_sha256(), _open_dir_no_follow() (+37 more)

### Community 28 - "run_subprocess"
Cohesion: 0.08
Nodes (38): _build_git_env(), gbrain_delete(), gbrain_sources_list(), gbrain_sync_full(), gbrain_sync_incremental(), gbrain_untag(), GbrainError, GbrainPageNotFound (+30 more)

### Community 30 - "runner.py"
Cohesion: 0.12
Nodes (27): evaluate_gate(), merge_thresholds(), Path, Activation gate policy for the Mnemosyne Portuguese retrieval quality gate.…, Evaluate the activation gate against an aggregate run. Returns a dict with…, Return a copy of ``defaults`` with any ``overrides`` applied. Only keys already…, evaluate_run(), Aggregate a full run: overall metrics, difficulty slices, latency.… (+19 more)

### Community 40 - "josemar-browser-control"
Cohesion: 0.10
Nodes (36): josemar-browser-control script, acquire_lock(), APP_NAME, CDP_PORT, cdp_up(), CHROME_PID_FILE, DEFAULT_KEY_SUFFIX, DEFAULT_KNOWN_HOSTS_SUFFIX (+28 more)

### Community 49 - "Mnemosyne Encrypted Backup Operations (Phase 3)"
Cohesion: 0.06
Nodes (34): 1. Download step (rclone image, `recovery` profile), 2. Verify step (hermes image, no rclone credentials), 3. Install step (operator-only, writers stopped), Architecture, Atomicity, Backup-mode rclone validation, Boundary contract, Compose-file ordering (+26 more)

### Community 50 - "Any"
Cohesion: 0.11
Nodes (36): build_create_markdown(), _extract_modeled_fields(), _normalize_semantic_frontmatter(), _normalize_semantic_text(), Any, Build markdown for a new task (no existing page)., Return frontmatter without write-through provenance keys., Ignore serializer-only boundary newlines while preserving text content. (+28 more)

### Community 55 - "mnemosyne_retrieval_eval/__init__.py"
Cohesion: 0.13
Nodes (20): Mnemosyne Portuguese vector retrieval quality harness (Phase 2). Stdlib-only…, difficulty_slices(), evaluate_query(), latency_percentiles(), mrr(), ndcg_at_k(), _rank_of(), Retrieval metrics for the Mnemosyne quality harness. Stdlib only. Pure… (+12 more)

### Community 58 - "check_versions"
Cohesion: 0.48
Nodes (6): check_versions(), main(), parse_requirements(), Path, Parse a strict ``name==version`` manifest into (name, version) pairs. Full-line…, Compare installed versions against the manifest. ``version_lookup`` is…

### Community 66 - "Obsidian Operations Runbook"
Cohesion: 0.06
Nodes (31): 1.1) Ensure server sidecar is connected, 1.2) Ensure Tailscale survives laptop reboots, 1) Install Tailscale on laptop, 2) Install Syncthing on laptop, 3) Start Syncthing on laptop, 4) Open server Syncthing UI, 5) Add server on laptop, 6) Accept device on server (+23 more)

### Community 69 - "main.py"
Cohesion: 0.16
Nodes (22): JobStateError, RuntimeError, QueueFullError, Raised when a CAS transition is rejected because the current state does not…, cancel_job(), _detect_cgroup_memory_limit_mb(), get_job(), health() (+14 more)

### Community 71 - "EvalRuntime"
Cohesion: 0.13
Nodes (15): EvalRuntime, generate_incontainer_script(), get_report_dir(), make_disposable_input(), CompletedProcess, Path, Copy a dataset into a fresh disposable temp input directory. Returns…, Generate the Python script that runs inside the Hermes container. ``mode`` is… (+7 more)

### Community 72 - "vault-recovery-uploader.sh"
Cohesion: 0.17
Nodes (26): acquire_upload_lock(), append_uploaded_ledger(), _cleanup(), is_valid_generation_id(), ledger_has(), log_error(), log_info(), main() (+18 more)

### Community 73 - "tasknotes_mcp.py"
Cohesion: 0.16
Nodes (29): _assert_runtime_identity(), _engine_call(), _env_float(), _get_engine(), main(), _mutation_dict(), Any, Create one task. When slug is omitted, a timestamp-prefixed slug is auto-… (+21 more)

### Community 78 - "Path"
Cohesion: 0.14
Nodes (28): _apply_all_sidecars_and_policy_unlocked(), apply_sidecar_and_enforce_policy(), _cli_apply(), _cli_apply_models(), _cli_migrate(), _cli_resolve(), _default_sidecar_path(), _iter_reconcilable_profiles() (+20 more)

### Community 84 - "LlamaRouterClient"
Cohesion: 0.23
Nodes (5): LlamaRouterClient, Path, RuntimeError, RouterError, Response

### Community 85 - "GBrain Operations Runbook"
Cohesion: 0.07
Nodes (26): Bundled Pack Fallback, Cron Pause/Resume for Maintenance Windows, Doctor Warns in No-Embedding Mode, Environment Defaults, GBrain Operations Runbook, gbrain Upgrade Checklist, Intended operator flow, Issue #110: Safe gbrain Adapter — Access Non-Negotiables (+18 more)

### Community 86 - "josemar-gbrain"
Cohesion: 0.23
Nodes (23): josemar-gbrain script, acquire_tasknotes_lock(), do_disable_embeddings(), do_embed_backfill(), do_enable_embeddings(), do_refresh(), do_refresh_embeddings(), do_reindex() (+15 more)

### Community 87 - "schema.py"
Cohesion: 0.19
Nodes (25): main(), Operator helper for printing the immutable Mnemosyne eval identity., dataset_fingerprint(), DatasetError, is_activation_dataset(), is_review_ready(), load_corpus(), load_manifest() (+17 more)

### Community 91 - "transcribe_granite.py"
Cohesion: 0.15
Nodes (25): _build_prompt(), _chunk_ranges(), _collapse_repeated_phrase_loops(), _extract_text_from_completion(), _find_overlap_word_count(), _guess_mime_type(), _merge_pair(), _merge_transcripts() (+17 more)

### Community 94 - "Remote Browser Control"
Cohesion: 0.08
Nodes (24): Architecture and data flow, Daily use, Disable / rollback, Enablement (GitHub variables), Laptop setup, Linux Mint (tested/supported) — repo on-demand launcher, macOS (untested, best-effort), Manual commands (diagnostic/fallback) (+16 more)

### Community 102 - "Josemar Assistente"
Cohesion: 0.09
Nodes (22): 1. Clone and Prepare State, 2. Configure `.env`, 3. Start Locally, 4. Optional Aux-ML, Agent State Sync, Architecture, Credentials, Development (+14 more)

### Community 103 - "mnemosyne-backup-uploader.sh"
Cohesion: 0.21
Nodes (19): acquire_upload_lock(), append_uploaded_ledger(), cleanup(), is_valid_generation_id(), log_error(), log_info(), main(), path_under_staging() (+11 more)

### Community 110 - "docker-hermes-init.sh"
Cohesion: 0.24
Nodes (19): activate_mnemosyne(), apply_sidecars_and_policy(), bridge_gbrain_api_keys(), cleanup_mnemosyne_artifacts(), install_gbrain_embedding_refresh_cron(), install_gbrain_refresh_cron(), install_mnemosyne_backup_export_cron(), install_vault_recovery_export_cron() (+11 more)

### Community 111 - "Vault Recovery Operations (Phase 1 + Phase 2 + Phase 3)"
Cohesion: 0.10
Nodes (20): Copy and convergence semantics, Disaster-recovery drill (Docker-gated, mandatory release gate), Environment, Generation layout, Goal and design (from the DR plan), Manifest, Migration sequence (operator-run, one time), Operations (+12 more)

### Community 112 - "Any"
Cohesion: 0.26
Nodes (17): load_models_state(), Any, Recursively reject any secret-looking keys anywhere in ``node``., Validate a parsed models.yaml document against the strict v1 schema. Returns…, Load and validate ``models.yaml``. Returns ``None`` if absent. Malformed YAML…, Parse and validate a models.yaml document from raw text. Returns the validated…, _reject_unknown_keys(), _validate_auxiliary() (+9 more)

### Community 113 - "Actions"
Cohesion: 0.10
Nodes (20): Actions, Conflicts with Josemar's setup (do NOT use), `gbrain backlinks`, `gbrain capture`, `gbrain get`, `gbrain link`, `gbrain put`, `gbrain query --no-expand` (opt-in semantic/hybrid retrieval, issue #65) (+12 more)

### Community 123 - "Memory & Embeddings Evaluation (Issues #86 / #65)"
Cohesion: 0.11
Nodes (18): Accuracy Caveats and Upstream Risks, Alternatives and caveats, Benchmark Evidence, Contents, Current Branch Scope and Status, Default recommendation: Brazilian Portuguese (pt-BR) on Josemar hardware, Embedding Model Selection, General selection guidance (+10 more)

### Community 124 - "vault-recovery-recover.sh"
Cohesion: 0.25
Nodes (14): cmd_download(), cmd_list_remote(), is_valid_generation_id(), log_error(), log_info(), main(), manifest_schema_validate(), prepare_active_config() (+6 more)

### Community 125 - "aux-ml"
Cohesion: 0.20
Nodes (18): action_cancel_job(), action_health(), action_job_status(), action_ocr_file(), action_queue_status(), action_submit_job(), action_wait_for_job(), _cancel_job() (+10 more)

### Community 135 - "ModelRegistry"
Cohesion: 0.22
Nodes (3): ModelRegistry, ModelSpec, Path

### Community 136 - "TaskNotes MCP Operations"
Cohesion: 0.11
Nodes (17): 1. Verify the existing gbrain Git repository, 2. Exclude `.git/` from Syncthing, 3. Compatible TaskNotes profile, 4. Gbrain source routing, Access non-negotiables (issue #110), Current limitations, External prerequisites, Known gbrain slug limitation (+9 more)

### Community 137 - "josemar_skill_state.py"
Cohesion: 0.12
Nodes (17): apply_state_to_config(), _canonical_profile_name(), config_has_toggle_keys(), empty_state(), enforce_policy(), extract_state_from_config(), _is_secret_looking_key(), policy_violations() (+9 more)

### Community 138 - "tasknotes_lock_run.py"
Cohesion: 0.18
Nodes (17): _acquire_lock(), _group_cleared(), _inherited_lock_fd(), _kill_group(), _parser(), ArgumentParser, Path, Popen (+9 more)

### Community 142 - "report.py"
Cohesion: 0.25
Nodes (8): Path, Report building and writing for the Mnemosyne retrieval harness. Stdlib only.…, Write report.json and report.md under out_dir. Returns the paths., Replace any raw text with a fixed placeholder. Reports must not include…, Render a concise Markdown summary of the report., redact_activation_text(), render_markdown(), write_report()

### Community 143 - "Browser Control First-Time Setup"
Cohesion: 0.12
Nodes (16): 1. Generate an SSH keypair on the laptop, 2. Add the public key as the `BROWSER_TUNNEL_AUTHORIZED_KEY` secret, 3. Allow the laptop to reach tcp:2222 on the server (Tailscale ACL), 4. Enable the overlay on the server, 5. Install the laptop launcher (Linux Mint, tested), 6. Start the browser and tunnel, 7. Verify end-to-end, Browser Control First-Time Setup (+8 more)

### Community 144 - "Available Actions"
Cohesion: 0.12
Nodes (16): Authentication, Available Actions, commit, Compatibility Protocols, diff, Environment Variables, gh, Invocation (+8 more)

### Community 156 - "AuxMLService"
Cohesion: 0.10
Nodes (7): AuxMLService, JobCancelledError, Raised when a running job is cancelled by an explicit cancel request., Worker-owned cleanup sequence after a running job is cancelled. Order: the job…, Unload the model or the in-flight loading target. Used by the cancellation…, Best-effort unload used by the normal worker cycle and shutdown., Explicit shutdown. Sets the stopping flag, cancels the worker task, cancels any…

### Community 157 - "Mnemosyne Portuguese Retrieval Quality Gate (Phase 3)"
Cohesion: 0.12
Nodes (15): Activation Gate Thresholds, E5 Prefix Behavior, FaQuAD-IR activation gate thresholds, Gate Statuses, Isolation Guarantees, Language Scope: the pt-BR Validated Baseline, Makefile Targets, Metrics (+7 more)

### Community 163 - "AGENTS.md"
Cohesion: 0.12
Nodes (14): Agent State Repo Rules, Directory Structure, gbrain Safe-Access Non-Negotiables (issue #110), Git Workflow, Graphify (codebase navigation, issue #116), Key References, Local Development, Project Overview (+6 more)

### Community 164 - "Auxiliary ML Service (`aux-ml`)"
Cohesion: 0.13
Nodes (14): API Endpoints, Auxiliary ML Service (`aux-ml`), Enabling the Service, Extending to New Models, File Handoff, Goals, Job Cancellation, Job Schema (OCR) (+6 more)

### Community 165 - "pii_guard.py"
Cohesion: 0.30
Nodes (14): Pattern, _collect_findings(), _digits(), Finding, _is_example_email(), _load_allowlist(), _luhn_valid(), main() (+6 more)

### Community 166 - "save_disabled_skills_stateful"
Cohesion: 0.20
Nodes (10): _active_hermes_home(), _atomic_write(), _native_save_config(), Josemar replacement for ``skills_config.save_disabled_skills``. Mutates…, Return the currently active ``HERMES_HOME``. Honors the context-local override…, Invoke the upstream ``save_config`` from ``hermes_cli.config``., Atomically write ``content`` to ``path`` via temp+replace. Preserves the…, Atomically write a canonical sidecar to ``path``. (+2 more)

### Community 175 - "run_ocr_task"
Cohesion: 0.27
Nodes (11): _build_column_clips(), _extract_text_from_completion(), _guess_mime_type(), _merge_column_parts(), _ocr_image_bytes(), Event, Path, _resolve_safe_input_path() (+3 more)

### Community 176 - "install-launcher.sh"
Cohesion: 0.21
Nodes (12): APPS_DIR, BIN_DIR, DESKTOP_SRC, die(), INSTALLED_BIN, INSTALLED_DESKTOP, is_managed_desktop(), is_owned_symlink() (+4 more)

### Community 177 - "hermes-gbrain-embedding-refresh.py"
Cohesion: 0.24
Nodes (12): _env_float(), _group_cleared(), main(), Popen, Run in the forked child before exec (preexec_fn): unblock every signal so the…, Finite nonnegative float from the environment, else the default. NaN/inf/-inf…, True when the leader is reaped and no process remains in its group. Reaps the…, TERM the whole group, escalate to SIGKILL if it survives the grace window, then… (+4 more)

### Community 178 - "normalize_state"
Cohesion: 0.18
Nodes (13): _cli_show(), deserialize_sidecar(), _normalize_platform_disabled(), normalize_state(), _normalize_string_list(), Coerce a config value into a sorted, deduped list of non-empty strings., Coerce ``platform_disabled`` into a dict of sorted/deduped string lists., Build a canonical sidecar dict from raw config values. (+5 more)

### Community 179 - "Life Chronicle — Full Reference"
Cohesion: 0.15
Nodes (12): Backfill (Existing Pages), Common Workflows, Concept: Notes and Atoms, Eligible Page Types, Event Schema (Timeline Atom), Field reference, `gbrain day` output structure, Kind Taxonomy (+4 more)

### Community 185 - "GitHub Workflows Documentation"
Cohesion: 0.17
Nodes (11): Deploy Workflow Notes, GitHub Workflows Documentation, Manual gbrain embedding activation, Prerequisites, Privacy Workflow Notes, Prompt Language Policy, Required Secrets, Required Variables (+3 more)

### Community 186 - "apply_models_overlay"
Cohesion: 0.18
Nodes (12): apply_models_overlay(), apply_models_to_config(), _atomic_write_yaml(), _dump_yaml(), _merge_fallback_by_provider(), _models_sidecar_path(), Apply the models.yaml overlay to ``config_path`` under the shared lock. Loads…, Return ``<workspace>/hermes/models.yaml`` (canonical tracked state). (+4 more)

### Community 187 - "mnemosyne-backup-recover.sh"
Cohesion: 0.36
Nodes (10): clear_recovery_dir(), is_valid_generation_id(), log_error(), log_info(), main(), prepare_active_config(), require_remote(), mnemosyne-backup-recover.sh script (+2 more)

### Community 188 - "Actions"
Cohesion: 0.17
Nodes (11): Actions, Aux ML Skill, `cancel_job`, `health`, Important Notes, `job_status`, `ocr_file`, `queue_status` (+3 more)

### Community 189 - "TaskNotes"
Cohesion: 0.17
Nodes (11): Boundaries, Current limitations, Key configuration areas, Mutation outcomes, Naming convention, Plugin configuration, Task or reminder, TaskNotes (+3 more)

### Community 194 - "main"
Cohesion: 0.25
Nodes (10): _active_schema_pack(), _chat_subcommand_allowed(), _drop_root(), main(), _parser(), ArgumentParser, True when the argv is on the documented agent-facing surface. Top-level…, Become the hermes runtime user before the shared lock is touched. The drop is… (+2 more)

### Community 195 - "mnemosyne-backup-restore.sh"
Cohesion: 0.33
Nodes (7): cmd_install_restore(), cmd_verify_restore(), log_error(), log_info(), main(), mnemosyne-backup-restore.sh script, usage()

### Community 196 - "Josemar Agent State Template"
Cohesion: 0.18
Nodes (10): File Map, First-Time Bootstrap, Josemar Agent State Template, Mnemosyne Pilot: Archive Status of Memory Files, Security, Setup, Skill edit policy, Skill Ownership Model (+2 more)

### Community 205 - "vault-recovery-restore.sh"
Cohesion: 0.40
Nodes (7): cmd_install_recovery(), cmd_rollback(), cmd_verify_recovery(), log_error(), main(), vault-recovery-restore.sh script, usage()

### Community 211 - "generate_slug"
Cohesion: 0.22
Nodes (9): generate_slug(), _get_zoneinfo(), Slugify a human-readable title into a gbrain-safe slug segment. Lowercases,…, Generate a task slug from a title and the current timestamp. Format: ``YYYY-MM-…, Return a tzinfo for the given timezone name, falling back to UTC., Return today's date as YYYY-MM-DD in the configured TZ., slugify_title(), today_in_tz() (+1 more)

### Community 223 - "vendor_faquad_ir.py"
Cohesion: 0.57
Nodes (7): download(), Path, Vendor the pinned MTEB-BR/faquad-ir Parquet release as JSONL. This is a…, sha256(), transform(), validate_source_artifacts(), write_jsonl()

### Community 224 - "Status Observation Reference"
Cohesion: 0.25
Nodes (7): How to answer common questions, Never, Reading the output, Security boundary, Status Observation Reference, What it never reports, What the command reports

### Community 231 - "._verify_readback"
Cohesion: 0.33
Nodes (5): Canonical semantic document for comparison. Contains type, title, tags, and…, Return True if the gbrain and disk semantic documents agree. Compares type,…, Verify structured gbrain and strict disk semantic documents agree., semantic_documents_agree(), SemanticDocument

### Community 232 - "Recovery Checklist Reference"
Cohesion: 0.29
Nodes (6): 1. Lane selection (user-confirmed), 2. Generation selection (user-confirmed), Boundaries, Checklist, Recovery Checklist Reference, The two explicit confirmations

### Community 233 - "Custom User Fields"
Cohesion: 0.29
Nodes (6): Adding fields, Custom User Fields, Defining custom fields in the profile, Discovery, Per-type validation, Using custom fields in tasks

### Community 234 - "BOOT.md"
Cohesion: 0.29
Nodes (5): Core Capability Sanity Checks, First Run, Mnemosyne Pilot (Optional), Optional Service Checks, Safe Defaults

### Community 238 - "hermes-vault-recovery-export-cron.sh"
Cohesion: 0.33
Nodes (5): hermes-vault-recovery-export-cron.sh script, VAULT_RECOVERY_EXPORT_TIMEOUT, VAULT_RECOVERY_GROUP_DRAIN, VAULT_RECOVERY_KILL_GRACE, VAULT_RECOVERY_TIMEOUT_MARGIN

### Community 239 - "Browser Control Skill"
Cohesion: 0.33
Nodes (5): Browser Control Skill, Connection failures, Safety, When to use the browser, Workflow

### Community 240 - "browser-tunnel/entrypoint.sh"
Cohesion: 0.80
Nodes (4): die(), log(), require_ipv4(), entrypoint.sh script

### Community 241 - "hermes-gbrain-embedding-refresh-cron.sh"
Cohesion: 0.40
Nodes (4): GBRAIN_EMBED_REFRESH_GROUP_DRAIN, GBRAIN_EMBED_REFRESH_KILL_GRACE, GBRAIN_EMBED_REFRESH_TIMEOUT, hermes-gbrain-embedding-refresh-cron.sh script

### Community 243 - "Backup Operations Skill"
Cohesion: 0.40
Nodes (4): Backup Operations Skill, Hard boundaries, Recovery: operator-only, confirmation-gated human checklist, Status: the only sanctioned action

### Community 244 - "Josemar User Schema Pack"
Cohesion: 0.40
Nodes (4): Josemar User Schema Pack, See Also, Source-First Workflow, Structure

### Community 247 - "Laptop launcher support boundaries"
Cohesion: 0.50
Nodes (3): Laptop launcher support boundaries, Linux (tested/supported), macOS and Windows (untested, best-effort suggestions only)

### Community 248 - "plugin"
Cohesion: 0.40
Nodes (4): plugin, $schema, ./.opencode/plugins/graphify.js, ./.opencode/plugins/pii-commit-guard.mjs

## Knowledge Gaps
- **352 isolated node(s):** `LAUNCHER_SRC`, `DESKTOP_SRC`, `BIN_DIR`, `APPS_DIR`, `INSTALLED_BIN` (+347 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **15 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `validate_models_state_from_text()` connect `Any` to `workspace_sync.py`, `josemar_skill_state.py`?**
  _High betweenness centrality (0.010) - this node is a cross-community bridge._
- **Why does `_load_models_validator()` connect `workspace_sync.py` to `Any`?**
  _High betweenness centrality (0.010) - this node is a cross-community bridge._
- **Why does `AuxMLService` connect `AuxMLService` to `service.py`, `main.py`, `ModelRegistry`, `JobScheduler`, `LlamaRouterClient`?**
  _High betweenness centrality (0.004) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `AuxMLService` (e.g. with `lifespan()` and `_service()`) actually correct?**
  _`AuxMLService` has 8 INFERRED edges - model-reasoned connections that need verification._
- **What connects `LAUNCHER_SRC`, `DESKTOP_SRC`, `BIN_DIR` to the rest of the system?**
  _352 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `workspace_sync.py` be split into smaller, more focused modules?**
  _Cohesion score 0.0548258138206739 - nodes in this community are weakly interconnected._
- **Should `vault_recovery_core.py` be split into smaller, more focused modules?**
  _Cohesion score 0.050061050061050064 - nodes in this community are weakly interconnected._