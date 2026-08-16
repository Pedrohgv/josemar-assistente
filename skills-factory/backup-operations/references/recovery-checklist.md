# Recovery Checklist Reference

Recovery is a HUMAN CHECKLIST executed by the operator. Chat never performs
any step of it and has no execution capability for it. The agent guides the
checklist and collects explicit user confirmations.

## The two explicit confirmations

### 1. Lane selection (user-confirmed)

The user must explicitly choose the lane:

- the DEFAULT vault-recovery lane, or
- the MNEMOSYNE lane (only if the operator has enabled it).

Never infer the lane. If the user does not state it, ask.

### 2. Generation selection (user-confirmed)

The user must explicitly name the exact generation id. Chat NEVER silently
selects the most recent generation:

- Present the candidates observed in the local staging area as candidates
  only, labeled as a local staging observation.
- If the user describes a generation by time ("the one from last week"),
  present the matching candidates and ask the user to confirm the exact id.
- If the user has no id, point them to the operator, whose listing of
  available generations is authoritative — the remote side may hold more
  than local staging shows.
- Only the user's explicit choice counts.

## Checklist

1. Confirm intent: the user wants recovery and understands the current state
   will be replaced as part of the operator's procedure (the operator retains
   a safety copy and pauses all owned jobs and writers for the maintenance
   window).
2. Collect the lane: explicit user choice.
3. Collect the generation: explicit user choice of an exact id.
4. Restate the full selection (lane + generation id) and obtain an explicit
   "yes, proceed" from the user.
5. Hand off to the operator: pass the selected lane and generation id to the
   operator, who follows the lane's runbook:
   - `docs/vault-recovery-operations.md` for the default lane,
   - `docs/mnemosyne-operations.md` for the Mnemosyne lane.
6. WAIT for the operator's completion report. Do not perform, assist with,
   or accelerate any step. Do not report success until the operator does.
7. After completion, the user may ask for a fresh local status observation
   via `josemar-backup-status`; label it the same way (local staging
   observation only).

## Boundaries

- Chat has no execution capability for recovery, no remote access, and no
  staging mutation.
- Never silently select a generation; selection is always the user's explicit
  choice.
- Never present local staging observation as remote status.
- If the user asks chat to perform recovery directly, decline the execution
  part and offer this checklist with the operator handoff.
