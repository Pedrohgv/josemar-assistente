# Status Observation Reference

How to read `josemar-backup-status` and what it can and cannot tell you.

## What the command reports

`josemar-backup-status` is a read-only observation of the LOCAL STAGING AREA.
Depending on what the environment exposes, it reports:

- the staged generation ids and their timestamps,
- the most recent staged generation,
- how many generations are currently staged,
- per-generation local marker observations (whether the generation's READY
  marker and manifest are readable and internally consistent), and
- `truncated:true` when the observation is partial (traversal or output
  bounds were exceeded).

The command NEVER reports export scheduling or enablement state. Do not
claim the export job is enabled, disabled, or due to run — the output
carries no such information.

Treat the output as a snapshot. Do not assume fields that are absent, and do
not invent meaning for fields you do not understand. If the command is not
available or fails, say so plainly and do not substitute other commands.

## Reading the output

- `truncated:true` means the observation is PARTIAL: traversal or output
  bounds were exceeded, so the report may not list every staged generation
  or full file counts. Never present a truncated report as the full picture.
- An EMPTY result (no snapshots) is ambiguous: nothing may have been staged
  yet, or the staging area may have held no readable generations. A staging
  root that exists but cannot be read surfaces as an explicit failure
  instead. Never treat an empty result as proof that backups are absent or
  broken.
- Each snapshot's marker observation (`local_ready_manifest_observation.ready` /
  `local_ready_manifest_observation.manifest`) is a LOCAL integrity signal
  only: the marker files are readable and consistent with the generation id
  (`generation_id`). It is not proof that the backup content is complete or
  usable, and it says nothing about the encrypted remote.

## What it never reports

- It NEVER reports the encrypted remote: whether a generation was sent to the
  remote, whether the remote is reachable or healthy, or what the remote
  retains.
- Remote status is UNKNOWN from chat and OPERATOR-ONLY. Only the operator can
  observe the remote side.
- A gap in local generations (for example, no new generation for a day) is
  NOT proof of a problem: a skipped run or a busy-lock skip happens silently
  and is only visible in operator logs.

## How to answer common questions

- "Are my backups safe?" → Report the local staging observation as exactly
  that — a LOCAL STAGING OBSERVATION — and state that remote status is
  unknown from chat; the operator can confirm it.
- "Did the backup run?" → Report what the local observation shows (the most
  recent staged generation and its timestamp), with the caveats above.
- "Is recovery possible from an older generation?" → Recovery is
  operator-handled; follow the `references/recovery-checklist.md` reference.

## Never

- Never equate local staging with remote safety.
- Never guess or imply remote state from local data.
- Never present a truncated or empty observation as a complete or definitive
  one.
- Never suggest, reproduce, or improvise any operational step.

## Security boundary

`josemar-backup-status` and this skill are a capability boundary, not a
complete security boundary against a compromised same-UID container/shell:
defense in depth, not a complete security boundary; do not overstate
protection.
