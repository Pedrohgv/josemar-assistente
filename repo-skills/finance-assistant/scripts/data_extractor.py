"""
Data extraction module for PDF invoice processing.
Extracts total expense and transaction details from credit card invoice text.
"""

import re
from typing import Dict, Any


def extract_expense_data(raw_invoice_text: str) -> Dict[str, Any]:
    """
    Extract total expense and expense details from credit card invoice text.
    
    Args:
        raw_invoice_text: Raw text from the invoice PDF
        
    Returns:
        Dictionary containing total_expense, expense_details, and line_count
    """
    # Pattern to find total expense
    total_patterns = [
        r'Valor total da Fatura:\s*R\$\s*([\d\.]+,\d{2})',
        r'O total da sua fatura é:\s*R\$\s*([\d\.]+,\d{2})',
        r'PEDRO HENRIQUE GOMES VENTUROTT\s*([\d\.]+,\d{2})'  # Name followed by total
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
