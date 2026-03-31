import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TOTAL_PATTERNS = [
    r'Valor total da Fatura:\s*R\$\s*([\d\.]+,\d{2})',
    r'O total da sua fatura é:\s*R\$\s*([\d\.]+,\d{2})',
    r'PEDRO HENRIQUE GOMES VENTUROTT\s*([\d\.]+,\d{2})',
]


def parse_brl(amount_str):
    return float(amount_str.replace('.', '').replace(',', '.'))


def extract_total(raw_text):
    for pattern in TOTAL_PATTERNS:
        match = re.search(pattern, raw_text)
        if match:
            return match.group(1)
    return None


def extract_expenses(raw_text, total_expense_str):
    lines = raw_text.split('\n')
    expense_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "PAGAMENTO DB DIRETO CONTA" in line:
            continue
        if re.match(r'^\d{2}/\d{2}\s+', line) and re.search(r'(-?[\d\.]+,\d{2})$', line):
            expense_lines.append(line)

    total_amount = parse_brl(total_expense_str) if total_expense_str else 0.0
    parsed = []
    sum_expenses = 0.0

    for line in expense_lines:
        date_match = re.match(r'^(\d{2}/\d{2})', line)
        amount_match = re.search(r'(-?[\d\.]+,\d{2})$', line)

        if not date_match or not amount_match:
            continue

        date_str = date_match.group(1)
        amount_str = amount_match.group(1)
        amount = parse_brl(amount_str)

        desc_start = len(date_str) + 1
        desc_end = line.rfind(amount_str)
        description = line[desc_start:desc_end].strip()

        day, month = map(int, date_str.split('/'))
        year = datetime.now().year - 1 if month == 12 else datetime.now().year

        parsed.append({
            'date': f"{day:02d}/{month:02d}/{year}",
            'description': description,
            'amount': amount,
        })
        sum_expenses += amount

    if total_amount > 0:
        tolerance = 0.01
        if abs(sum_expenses - total_amount) <= tolerance:
            logger.info(f"Sum matches total: {sum_expenses:.2f} == {total_amount:.2f}")
        else:
            logger.warning(
                f"Sum ({sum_expenses:.2f}) != total ({total_amount:.2f}), "
                f"diff: {abs(sum_expenses - total_amount):.2f}"
            )

    return parsed
