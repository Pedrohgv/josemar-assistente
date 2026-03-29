#!/usr/bin/env python3
"""
PDF extraction skill for OpenClaw.
Extracts text from PDF files and processes credit card invoice data.
"""

import sys
import json
import re
import pymupdf
import traceback
from typing import Dict, List, Any, Optional

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF file using pymupdf."""
    try:
        doc = pymupdf.open(pdf_path)
        text = ''
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        raise Exception(f"Error extracting text from PDF: {str(e)}")

def parse_brazilian_currency(amount_str: str) -> float:
    """Convert Brazilian currency format to float."""
    # Remove R$ symbol, dots (thousands separator), and replace comma with dot
    cleaned = amount_str.replace('R$', '').strip()
    cleaned = cleaned.replace('.', '').replace(',', '.')
    return float(cleaned)

def extract_expense_data(raw_invoice_text: str) -> Dict[str, Any]:
    """
    Extract total expense and expense details from credit card invoice text.
    Based on the reference implementation from josemar-agente-despesas.
    """
    # Pattern to find total expense
    total_patterns = [
        r'Valor total da Fatura:\s*R\$\s*([\d\.]+,\d{2})',
        r'O total da sua fatura é:\s*R\$\s*([\d\.]+,\d{2})',
        r'PEDRO HENRIQUE GOMES VENTUROTT\s*([\d\.]+,\d{2})'
    ]
    
    total_expense = None
    for pattern in total_patterns:
        match = re.search(pattern, raw_invoice_text)
        if match:
            total_expense = match.group(1)
            break
    
    # Extract expense details - look for transaction lines
    expense_lines = []
    lines = raw_invoice_text.split('\n')
    
    for line in lines:
        line = line.strip()
        # Look for lines that start with date pattern (dd/mm) and contain an amount at the end
        if re.match(r'^\d{2}/\d{2}\s+', line):
            # Check if line ends with an amount pattern
            amount_match = re.search(r'([\d\.]+,\d{2})$', line)
            if amount_match:
                expense_lines.append(line)
    
    expense_details = '\n'.join(expense_lines)
    
    return {
        'total_expense': total_expense,
        'expense_details': expense_details,
        'line_count': len(expense_lines)
    }

def parse_expense_details(expense_details: str, total_expense: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parse expense details into structured data.
    Simplified version without classification.
    """
    parsed_expenses = []
    lines = expense_details.strip().split('\n')
    
    for line in lines:
        if not line.strip():
            continue
        
        # Skip payment entries
        if "PAGAMENTO DB DIRETO CONTA" in line:
            continue
        
        # Extract date, description, and amount
        # Pattern: date (dd/mm) followed by description and amount at the end
        match = re.match(r'^(\d{2}/\d{2})\s+(.+?)\s+([\d\.]+,\d{2})$', line)
        if match:
            date, description, amount = match.groups()
            parsed_expenses.append({
                'date': date,
                'description': description.strip(),
                'amount': amount,
                'amount_float': parse_brazilian_currency(amount)
            })
    
    return parsed_expenses

def main():
    """Main entry point for the PDF extraction skill."""
    try:
        # Read input from stdin (OpenClaw passes input via stdin)
        input_data = sys.stdin.read().strip()
        
        if not input_data:
            # No input provided, return usage information
            result = {
                'error': 'No input provided',
                'usage': 'Provide a PDF file path or text content',
                'example': 'echo "/path/to/file.pdf" | python pdf_extractor.py'
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(1)
        
        # Check if input is a file path
        if input_data.endswith('.pdf'):
            # Extract text from PDF file
            pdf_text = extract_text_from_pdf(input_data)
            extracted_data = extract_expense_data(pdf_text)
            
            # Parse expense details
            parsed_expenses = []
            if extracted_data['expense_details']:
                parsed_expenses = parse_expense_details(
                    extracted_data['expense_details'],
                    extracted_data['total_expense']
                )
            
            result = {
                'success': True,
                'source': 'pdf_file',
                'file_path': input_data,
                'extraction': {
                    'total_expense': extracted_data['total_expense'],
                    'line_count': extracted_data['line_count'],
                    'text_preview': pdf_text[:500] + '...' if len(pdf_text) > 500 else pdf_text
                },
                'parsed_expenses': parsed_expenses,
                'summary': {
                    'total_items': len(parsed_expenses),
                    'total_amount': sum(exp['amount_float'] for exp in parsed_expenses) if parsed_expenses else 0
                }
            }
        else:
            # Assume input is raw text
            extracted_data = extract_expense_data(input_data)
            parsed_expenses = []
            if extracted_data['expense_details']:
                parsed_expenses = parse_expense_details(
                    extracted_data['expense_details'],
                    extracted_data['total_expense']
                )
            
            result = {
                'success': True,
                'source': 'raw_text',
                'extraction': {
                    'total_expense': extracted_data['total_expense'],
                    'line_count': extracted_data['line_count']
                },
                'parsed_expenses': parsed_expenses,
                'summary': {
                    'total_items': len(parsed_expenses),
                    'total_amount': sum(exp['amount_float'] for exp in parsed_expenses) if parsed_expenses else 0
                }
            }
        
        # Output result as JSON
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    except Exception as e:
        error_result = {
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }
        print(json.dumps(error_result, indent=2, ensure_ascii=False))
        sys.exit(1)

if __name__ == '__main__':
    main()
