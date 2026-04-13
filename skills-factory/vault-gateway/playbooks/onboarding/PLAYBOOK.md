# Onboarding Playbook

## Status

- State: active
- Source: `dump_folder/My-Brain-Is-Full-Crew/skills/onboarding/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Initialize a new vault with standard baseline structure.
- Support deterministic "port existing vault" workflow.
- Enforce destructive safety gate with explicit backup confirmation.

## Flow

1. Ask user to choose:
   - `novo vault`
   - `port existing vault`
2. If `novo vault`, ask one confirmation before creating baseline structure.
3. If `port existing vault`, ask if port should be destructive.
4. If destructive:
   - emit strong warning
   - strongly recommend backup
   - require exact confirmation phrase
   - show plan
   - require final execution confirmation phrase
5. If non-destructive:
   - show plan
   - require exact approval phrase before execution

## Safety Gates

- Backup confirmation phrase: `eu tenho backup e quero continuar`
- Non-destructive execution phrase: `aprovar port nao destrutivo`
- Destructive execution phrase: `executar port destrutivo`

## Deterministic Plan Output

- Whether vault exists
- Missing baseline directories
- Non-standard top-level entries
- Exact action list for selected mode

## Execution Behavior

- Non-destructive: create missing baseline dirs/files only.
- Destructive: non-destructive baseline + move non-standard root entries into `04-Archive/Imported-Root-<timestamp>`.
- Every execution appends to `Meta/vault-gateway-log.md`.
