---
name: pdf-extractor
description: Extracts text and expense data from Brazilian credit card invoice PDFs
categories:
  - pdf
  - finance
  - extraction
  - brazilian
---

# PDF Extractor Skill

This skill processes PDF files of Brazilian credit card invoices and extracts structured data.

## Description

The PDF extraction skill can:
- Extract total expense amount from credit card invoices
- Parse individual transaction details (date, description, amount)
- Return structured data in JSON format
- Process both PDF files and raw text content

## Usage

The skill accepts input via stdin:
1. **File path**: Provide a path to a PDF document
2. **Raw text**: Provide text content extracted from a PDF

### Examples

```bash
# Process a PDF file
echo "/path/to/invoice.pdf" | pdf-extractor

# Process raw text
cat invoice.txt | pdf-extractor
```

## Input

The skill reads from stdin:
- **File path**: String ending with `.pdf`
- **Raw text**: Any text content (typically from PDF invoices)

## Output

Returns JSON structure containing:
- `success`: boolean indicating success/failure
- `source`: `"pdf_file"` or `"raw_text"`
- `extraction`: Metadata about extraction
  - `total_expense`: Total expense amount found
  - `line_count`: Number of transaction lines found
  - `text_preview` (for PDF files): First 500 characters of extracted text
- `parsed_expenses`: Array of expense objects with:
  - `date`: Transaction date (dd/mm)
  - `description`: Transaction description
  - `amount`: Amount as string (R$ format)
  - `amount_float`: Amount as float (for calculations)
- `summary`: Aggregated statistics
  - `total_items`: Number of parsed expenses
  - `total_amount`: Sum of all expense amounts

## Supported Invoice Formats

This skill is designed for Brazilian credit card invoices with:
- **Total patterns**: "Valor total da Fatura", "O total da sua fatura é", or account name
- **Transaction lines**: Format: `dd/mm <description> <amount>`
- **Currency**: Brazilian format (R$ with comma as decimal separator)
- **Example line**: `10/12 UBER TRIP 32,75`

## Error Handling

The skill handles:
- Missing input (returns usage information)
- Invalid PDF files (returns error with traceback)
- Parsing errors (continues with available data)
- Payment entries (excludes "PAGAMENTO DB DIRETO CONTA")

## Dependencies

- Python 3
- pymupdf library (for PDF text extraction)

## Notes

- The skill uses the same extraction logic as the reference josemar-agente-despesas assistant
- Brazilian currency format is properly handled (thousands separator: dot, decimal: comma)
- Payment entries are automatically filtered out from results
- Date parsing assumes dd/mm format
