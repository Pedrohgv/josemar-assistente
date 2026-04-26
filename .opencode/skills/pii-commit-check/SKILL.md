---
name: pii-commit-check
description: Run staged PII checks before creating commits.
compatibility: opencode
---

# PII Commit Check

Use this skill whenever preparing a commit.

## Workflow

1. Run deterministic scan on staged changes:

```bash
python3 scripts/pii_guard.py --staged --fail-on medium
```

2. Run manual agentic review in the current OpenCode session (uses current configured model/provider, no extra API key):

- Ask OpenCode to inspect staged diff for PII risks before commit.
- Example prompt:
  - "Review `git diff --cached` for possible PII leaks (CPF/CNPJ/phone/email/card/personal identifiers). Flag high-confidence risks and suggest redactions."

3. If any check fails:
- Block commit.
- Show offending lines/findings.
- Ask for redaction or explicit allowlist update in `.pii-allowlist`.

4. Only proceed with `git commit` after all checks pass.
