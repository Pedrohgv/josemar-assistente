---
name: backup-operations
description: Observe encrypted backup status and coordinate recovery for the backup lanes (default vault-recovery lane, optional Mnemosyne lane). The only sanctioned status action is the read-only `josemar-backup-status` local staging observation; remote status is unknown to chat and operator-only, and recovery is a confirmation-gated human checklist. Use when the user asks about backup status, whether backups are running, backup safety, or recovery from a backup.
categories:
  - backup
  - recovery
  - operations
---

# Backup Operations Skill

Guidance for agent-facing backup work. Two encrypted backup lanes exist:

- **Default lane** — the vault-recovery lane: daily immutable generations of
  the Obsidian vault and the gbrain state tree, staged locally and sent to an
  encrypted remote.
- **Optional lane** — the Mnemosyne lane, only when the operator has enabled
  it: encrypted backups of the Mnemosyne memory store.

Both lanes are driven by owned export jobs and separate operator-side
services. Chat has no direct access to either lane's machinery.

## Status: the only sanctioned action

The ONLY backup command chat may invoke is the read-only status command
`josemar-backup-status`. Use it to observe the local staging area.

**Label every report from it as a LOCAL STAGING OBSERVATION ONLY.**

- It reflects what is visible in the local staging area: staged generations,
  their timestamps, and per-generation local marker observations. It never
  reports export scheduling or enablement state — do not claim either.
- `truncated:true` in the output means the observation is PARTIAL: traversal
  or output bounds were exceeded, so the report may not be complete. Do not
  treat a truncated report as the full picture.
- An empty result (no snapshots) is AMBIGUOUS: nothing may have been staged
  yet, or the staging area may not have been readable. It is never proof
  that backups are absent or broken.
- Each snapshot's marker observation (the generation's READY marker and
  manifest readable and internally consistent) is a LOCAL integrity signal
  only. It says nothing about the encrypted remote and is not proof the
  backup content is usable.
- **Remote status is UNKNOWN to chat and OPERATOR-ONLY.** The command cannot
  observe the encrypted remote: whether a generation reached the remote,
  whether the remote is healthy, or what the remote retains. Never claim a
  backup is safe or current based on local output alone.
- A missing recent generation is not proof of a problem: a skipped or
  busy-lock run is visible only in operator logs.
- If the command is unavailable or fails, report that plainly; do not
  improvise other backup commands — none exist for chat.

Full interpretation guidance:
`skill_view("backup-operations", file_path="references/status-observation.md")`.

## Recovery: operator-only, confirmation-gated human checklist

Chat NEVER performs recovery. There is no execution capability for it and no
recovery command exists for chat. Recovery is a HUMAN CHECKLIST executed by
the operator; the agent guides the checklist and collects explicit user
confirmations.

Two explicit user confirmations are required before any handoff:

1. **Lane selection.** The user must explicitly choose the lane: the default
   vault-recovery lane or the Mnemosyne lane. Never infer it; ask.
2. **Generation selection.** The user must explicitly name the exact
   generation id to use. NEVER silently select the latest generation: if the
   user does not name one, present the candidates observed in the local
   staging area and ask the user to choose. Only the user's explicit choice
   counts.

Then:

3. Restate the full selection (lane + exact generation id) and obtain an
   explicit "yes, proceed" from the user.
4. Hand the selection to the operator, who follows the selected lane's
   runbook, including pausing all owned jobs and writers for the maintenance
   window.
5. Wait for the operator's completion report. Do not perform, assist with, or
   report any step yourself.

Operator runbooks (authoritative, operator-executed):

- `docs/vault-recovery-operations.md` — default lane
- `docs/mnemosyne-operations.md` — Mnemosyne lane

Full checklist:
`skill_view("backup-operations", file_path="references/recovery-checklist.md")`.

## Hard boundaries

- The ONLY chat-visible backup command is `josemar-backup-status`.
- Never suggest, reproduce, or improvise any operational backup or recovery
  step from chat: no container, no remote, no staging mutation, no handoff
  steps. The operator runbooks above are the only authoritative procedures.
- Never present local staging observation as remote backup status.
- Never silently select a generation; selection is always the user's explicit
  choice.
- If the user asks chat to perform recovery directly, decline the execution
  part and offer the checklist with the operator handoff.
- **Defense in depth, not a complete security boundary.** `josemar-backup-status`
  and this skill are a capability boundary, not a complete security boundary
  against a compromised same-UID container/shell; do not overstate
  protection.
