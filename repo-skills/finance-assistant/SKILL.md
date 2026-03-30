---
name: finance-assistant
description: Complete financial tracking assistant for credit card expenses and general expenses. Extracts data from Brazilian credit card PDF invoices, classifies expenses using regex patterns, and manages Google Sheets tables for expense tracking. Supports historical queries and month-based organization.
categories:
  - finance
  - pdf
  - google-sheets
  - brazilian
  - expenses
---

# Finance Assistant Skill

Complete financial tracking system for managing credit card expenses and general expenses using Google Sheets.

## Overview

This skill provides comprehensive expense management:
- Extract and classify expenses from Brazilian credit card PDF invoices
- Automatically categorize expenses using regex pattern matching
- Register expenses to Google Sheets tables (Cartão and Despesas)
- Manage persistent database of establishments and categories
- Support month-based organization with intelligent suggestions

## Billing Cycle Handling

**Important:** Credit card invoices follow billing cycles, not calendar months. When processing an invoice:

- **ALL extracted expenses go to the table you specify** - regardless of their individual transaction dates
- This includes:
  - Purchases from the current billing period
  - Purchases from previous months (pending charges that appeared on this invoice)
  - **Refunds and credits** (negative amounts are automatically summed)
- The "Data" column shows the original transaction date, but all entries belong to the billing month being updated

**Example:**
If you're updating "Cartão Jan" (January's table) but the invoice contains:
- 10 purchases from January
- 3 purchases from December (previous month, now charged)
- 1 refund/credit for R$ -50,00

**All 14 entries go to "Cartão Jan" table.** The total invoice amount (which includes the refund) is what matters for your tracking. The skill handles everything automatically - you just specify which month's table to update.

## Usage

### Extract Expenses from PDF

```bash
# Process PDF file
echo '{"action": "extract", "pdf_path": "/path/to/invoice.pdf"}' | finance-assistant

# Process raw text
echo '{"action": "extract", "text": "10/01 UBER TRIP 32,75\n15/01 SUPERMERCDO EXTRA 150,00"}' | finance-assistant
```

### Get Month Suggestion

```bash
# Get suggestion based on current date
echo '{"action": "suggest_month"}' | finance-assistant
```

**Month Selection Logic:**
- Days 1-10: Suggests last month (requires user confirmation)
- Days 11+: No suggestion, asks user to specify

### Register Credit Card Expenses

```bash
# After extraction, register to Google Sheets
echo '{
  "action": "register_credit_card",
  "expenses": {
    "Data": ["10/01/2025", "15/01/2025"],
    "Estabelecimento": ["Uber", "Supermercado"],
    "Categoria": ["Transporte", "Supermercado"],
    "Valor": [32.75, 150.00],
    "Texto Compra": ["UBER TRIP", "SUPERMERCADO EXTRA"]
  },
  "month": "Jan",
  "year": 2025
}' | finance-assistant
```

**Note:** Refunds and credits (negative values) are automatically included in the registration. The skill handles all transaction types - purchases, pending charges from previous months, and refunds - all go to the specified month's table.

### Register General Expense

```bash
# Update a general expense (like Aluguel, Internet)
echo '{
  "action": "register_general",
  "description": "Aluguel",
  "amount": 1800.00,
  "month": "Jan",
  "year": 2025
}' | finance-assistant
```

## Input/Output Format

### Extract Action Input

```json
{
  "action": "extract",
  "pdf_path": "/path/to/invoice.pdf"
}
```

Or with text:
```json
{
  "action": "extract",
  "text": "raw invoice text content"
}
```

### Extract Action Output

```json
{
  "success": true,
  "source": "pdf_file",
  "file_path": "/path/to/invoice.pdf",
  "extraction": {
    "total_expense": "1.234,56",
    "line_count": 15,
    "text_preview": "First 500 chars of extracted text..."
  },
  "classified_expenses": [
    {
      "date": "10/01",
      "full_date": "10/01/2025",
      "description": "UBER TRIP",
      "amount": 32.75,
      "establishment": "Uber",
      "category": "Transporte"
    }
  ],
  "sheets_data": {
    "Data": ["10/01/2025"],
    "Estabelecimento": ["Uber"],
    "Categoria": ["Transporte"],
    "Valor": [32.75],
    "Texto Compra": ["UBER TRIP"]
  },
  "summary": {
    "total_items": 15,
    "total_amount": 1234.56
  }
}
```

## Database

The skill uses SQLite for persistent storage of establishments and categories.

**Location:** `/root/.openclaw/workspace/finance_assistant.db`

### Predefined Categories

- **Supermercado** - Grocery expenses
- **Transporte** - Uber, 99 Pop, transportation services
- **Marmitas** - Day-to-day, healthy and frozen meals
- **Ifood** - Take-out and delivered food
- **Assinaturas** - Subscription services
- **Outros** - Miscellaneous expenses

### Establishments

Establishments are stored with regex patterns for automatic matching:
- `name`: Display name (e.g., "Uber")
- `match_pattern`: Regex pattern to match (e.g., "uber")
- `exclude_pattern`: Optional regex to exclude false positives
- `category_id`: Associated category

## Google Sheets Structure

### Organization

```
Spreadsheet: "{YEAR}" (e.g., "2025", "2026")
├── Worksheet: "Jan"
│   ├── Table: "Cartão Jan" (Credit card expenses)
│   └── Table: "Despesas Jan" (General expenses)
├── Worksheet: "Fev"
│   ├── Table: "Cartão Fev"
│   └── Table: "Despesas Fev"
└── ... (all 12 months)
```

### Cartão Table Schema

| Column | Type | Description |
|--------|------|-------------|
| Data | Date | Transaction date (dd/mm/yyyy) |
| Estabelecimento | Text | Merchant name |
| Categoria | Text | Expense category |
| Valor | Number | Amount in BRL |
| Texto Compra | Text | Original transaction description |

### Despesas Table Schema

| Column | Type | Description |
|--------|------|-------------|
| Despesa | Text | Expense description |
| Valor | Number | Amount in BRL |

## Historical Queries (gogcli)

For complex historical queries, use gogcli commands directly:

### List All Spreadsheets

```bash
gog sheets list
```

### List Tables in a Spreadsheet

```bash
gog sheets table list <spreadsheet-id>
```

### Get Table Data

```bash
# Get all data from a credit card table
gog sheets table get <spreadsheet-id> <table-id>

# Example: Get Cartão Jan from 2025 spreadsheet
# First get spreadsheet ID for "2025"
# Then get table ID for "Cartão Jan"
# Finally: gog sheets table get <spreadsheet-id> <table-id> --json
```

### Query by Establishment

```bash
# Export table to CSV and filter
gog sheets table get <spreadsheet-id> <table-id> --format csv > cartao_jan.csv
# Then filter by establishment using standard tools
```

### Query Multiple Months

```bash
# Script to query multiple months
for month in Jan Fev Mar; do
    echo "=== Cartão $month ==="
    gog sheets table get <spreadsheet-id> "Cartão $month" --json
done
```

### Calculate Aggregations

```bash
# Get all data and calculate sum
# This returns JSON that can be processed with jq or Python
gog sheets table get <spreadsheet-id> <table-id> --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
total = sum(float(row[3].replace('R$', '').replace('.', '').replace(',', '.')) for row in data['data'])
print(f'Total: R$ {total:.2f}')
"
```

## Consultation Examples

### Example 1: Total spent on Transporte in January 2025

```bash
# 1. Get spreadsheet ID for 2025
SPREADSHEET_ID=$(gog sheets list --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([s['id'] for s in d if s['name']=='2025'][0])")

# 2. Get Cartão Jan table ID
TABLE_ID=$(gog sheets table list $SPREADSHEET_ID --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([t['id'] for t in d if t['name']=='Cartão Jan'][0])")

# 3. Get data and filter by category
gog sheets table get $SPREADSHEET_ID $TABLE_ID --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
rows = data.get('data', [])
transporte_total = 0
for row in rows:
    if len(row) >= 4 and row[2] == 'Transporte':  # Categoria column
        valor = float(row[3].replace('R$', '').replace('.', '').replace(',', '.'))
        transporte_total += valor
print(f'Total em Transporte (Jan/2025): R$ {transporte_total:.2f}')
"
```

### Example 2: Average monthly spending on Aluguel

```bash
# Query Despesas tables across multiple months
YEAR=2025
SPREADSHEET_ID=$(gog sheets list --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([s['id'] for s in d if s['name']=='$YEAR'][0])")

for month in Jan Fev Mar Abr Mai Jun; do
    TABLE_NAME="Despesas $month"
    TABLE_ID=$(gog sheets table list $SPREADSHEET_ID --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([t['id'] for t in d if t['name']=='$TABLE_NAME'][0])" 2>/dev/null)
    if [ ! -z "$TABLE_ID" ]; then
        gog sheets table get $SPREADSHEET_ID $TABLE_ID --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for row in data.get('data', []):
    if len(row) >= 2 and row[0] == 'Aluguel':
        print(f'$month: {row[1]}')
"
    fi
done
```

### Example 3: Compare Supermercado expenses month-over-month

```bash
YEAR=2025
SPREADSHEET_ID=$(gog sheets list --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([s['id'] for s in d if s['name']=='$YEAR'][0])")

python3 << EOF
import subprocess
import json

months = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
results = {}

for month in months:
    try:
        # Get table ID
        result = subprocess.run(
            ["gog", "sheets", "table", "list", "$SPREADSHEET_ID", "--json"],
            capture_output=True, text=True
        )
        tables = json.loads(result.stdout)
        table_id = None
        for t in tables:
            if t['name'] == f"Cartão {month}":
                table_id = t['id']
                break
        
        if table_id:
            # Get table data
            result = subprocess.run(
                ["gog", "sheets", "table", "get", "$SPREADSHEET_ID", table_id, "--json"],
                capture_output=True, text=True
            )
            data = json.loads(result.stdout)
            total = 0
            for row in data.get('data', []):
                if len(row) >= 4 and row[2] == 'Supermercado':
                    valor = float(row[3].replace('R$', '').replace('.', '').replace(',', '.'))
                    total += valor
            results[month] = total
            print(f"{month}: R$ {total:.2f}")
    except Exception as e:
        print(f"{month}: Error - {e}")

# Calculate average
if results:
    avg = sum(results.values()) / len(results)
    print(f"\\nMédia: R$ {avg:.2f}")
EOF
```

## Month Formats

The skill accepts months in multiple formats (case-insensitive):

**Portuguese:**
- Full names: Janeiro, Fevereiro, Março, Abril, Maio, Junho, Julho, Agosto, Setembro, Outubro, Novembro, Dezembro
- Abbreviations: Jan, Fev, Mar, Abr, Mai, Jun, Jul, Ago, Set, Out, Nov, Dez

**English:**
- Full names: January, February, March, April, May, June, July, August, September, October, November, December
- Abbreviations: Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec

## Error Handling

The skill returns structured error responses:

```json
{
  "success": false,
  "error": "Descriptive error message",
  "details": "Additional context if available"
}
```

Common errors:
- Missing input
- Invalid JSON
- PDF extraction failure
- Spreadsheet not found
- Table not found
- Authentication issues (gogcli not authenticated)

## Dependencies

- Python 3
- pymupdf (PDF text extraction)
- gogcli (Google Sheets operations)

## Files

- `finance-assistant` - Main executable
- `scripts/database.py` - SQLite database operations
- `scripts/models.py` - Data models
- `scripts/data_extractor.py` - PDF text extraction
- `scripts/data_parser.py` - Expense parsing and validation
- `scripts/classifier.py` - Expense classification
- `scripts/establishment_matcher.py` - Regex pattern matching
- `scripts/pdf_processor.py` - PDF processing orchestration
- `scripts/sheets_manager.py` - Google Sheets interface
- `scripts/month_utils.py` - Month utilities and suggestions

## Notes

- Payment entries ("PAGAMENTO DB DIRETO CONTA") are automatically filtered out
- Brazilian currency format is properly handled (R$ 1.234,56)
- Year logic: December transactions are assigned to previous year
- Database persists across container restarts in workspace volume
