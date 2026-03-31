---
name: finance-assistant
description: Credit card expense tracking. Extracts expenses from Brazilian credit card PDF invoices, classifies them, and writes to Google Sheets. Single-action workflow: provide a PDF, month, and year.
categories:
  - finance
  - pdf
  - google-sheets
  - brazilian
  - expenses
---

# Finance Assistant Skill

Extracts expenses from Brazilian credit card PDF invoices, classifies them, and writes to Google Sheets in a single call.

## Agent Workflow

1. **Receive PDF** from user
2. **Determine month/year** before calling the skill:
   - Days 1-10 of month: likely last month's invoice, ask for confirmation
   - Days 11+: ask user which month to update
   - Year: usually current year; December invoices may close in January of next year
3. **Call the skill** with `register` action
4. **Report results** to user, highlighting any unclassified expenses

## Actions

### register (Primary)

Extract, classify, and write expenses to Google Sheets in one step.

```bash
echo '{
  "action": "register",
  "pdf_path": "/path/to/invoice.pdf",
  "month": "Jan",
  "year": 2025
}' | finance-assistant
```

**Output (success):**
```json
{
  "success": true,
  "rows_added": 15,
  "total_amount": 1234.56,
  "total_expense": "1.234,56",
  "table": "Cartao Jan",
  "spreadsheet": "2025",
  "unclassified": [
    {"description": "UNKNOWN MERCHANT", "amount": 50.0}
  ]
}
```

**Output (with unclassified):**
If any expenses don't match a known pattern, they are registered as "Outros" and listed in `unclassified`. The agent should inform the user so they can add new patterns if needed.

### add_establishment

Add a new regex pattern for classification.

```bash
echo '{
  "action": "add_establishment",
  "name": "iFood",
  "pattern": "ifood",
  "category": "Ifood"
}' | finance-assistant
```

Optional `exclude` field: regex to exclude false positives.

### remove_establishment

Remove an establishment pattern by name.

```bash
echo '{
  "action": "remove_establishment",
  "name": "iFood"
}' | finance-assistant
```

### list_establishments

```bash
echo '{"action": "list_establishments"}' | finance-assistant
```

### list_categories

```bash
echo '{"action": "list_categories"}' | finance-assistant
```

## Classification

Expenses are classified using regex patterns stored in JSON config files. If no pattern matches, the expense gets category "Outros" and the raw description as establishment name.

**Config location:** `/root/.openclaw/workspace/finance-config/`
- `categories.json` - array of category names
- `establishments.json` - array of `{name, pattern, category, exclude?}`

Config is created from defaults on first run and persists across container restarts. The agent can modify patterns via `add_establishment`/`remove_establishment`.

## Month Formats

Case-insensitive:
- **Portuguese:** Janeiro, Fevereiro, Março, ..., Jan, Fev, Mar, ...
- **English:** January, February, March, ..., Jan, Feb, Mar, ...

## Google Sheets Structure

```
Spreadsheet: "{YEAR}" (e.g., "2025")
├── Worksheet: "Jan"
│   ├── Table: "Cartao Jan" (credit card expenses)
│   └── Table: "Despesas Jan" (general expenses)
└── ...
```

### Cartao Table Schema

| Column | Type | Description |
|--------|------|-------------|
| Data | Date | Transaction date (dd/mm/yyyy) |
| Estabelecimento | Text | Merchant name |
| Categoria | Text | Expense category |
| Valor | Number | Amount in BRL |
| Texto Compra | Text | Original transaction description |

## Billing Cycle

ALL extracted expenses go to the specified month's table, regardless of individual transaction dates. This includes purchases from previous months and refunds/credits.

## Dependencies

- Python 3
- pymupdf
- gogcli (Google Sheets operations)
