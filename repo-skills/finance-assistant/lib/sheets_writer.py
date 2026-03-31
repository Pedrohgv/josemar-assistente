import subprocess
import json
import logging

logger = logging.getLogger(__name__)


def _run_gog(args):
    cmd = ["gog"] + args + ["--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode == 0:
        try:
            return {"success": True, "data": json.loads(result.stdout)}
        except json.JSONDecodeError:
            return {"success": True, "data": result.stdout}
    else:
        return {"success": False, "error": result.stderr or "Unknown error"}


def find_spreadsheet(year):
    result = _run_gog(["drive", "search", f"type:spreadsheet '{year}'"])
    if not result["success"]:
        logger.error(f"Failed to search spreadsheet: {result.get('error')}")
        return None, f"Failed to search Google Drive: {result.get('error')}"

    year_str = str(year)
    for file in result["data"].get("files", []):
        if file.get("name") == year_str:
            return file.get("id"), None

    return None, f"Spreadsheet '{year_str}' not found in Google Drive"


def find_table(spreadsheet_id, table_name):
    result = _run_gog(["sheets", "table", "list", spreadsheet_id])
    if not result["success"]:
        logger.error(f"Failed to list tables: {result.get('error')}")
        return None, f"Failed to list tables: {result.get('error')}"

    for table in result["data"].get("tables", []):
        if table.get("tableName") == table_name:
            return table.get("tableId"), None

    return None, f"Table '{table_name}' not found in spreadsheet"


def append_expenses(year, month_abbr, expenses):
    spreadsheet_id, err = find_spreadsheet(year)
    if err:
        return {"success": False, "error": err}

    table_name = f"Cartão {month_abbr}"
    table_id, err = find_table(spreadsheet_id, table_name)
    if err:
        return {"success": False, "error": err}

    if not expenses:
        return {
            "success": True,
            "rows_added": 0,
            "table": table_name,
            "spreadsheet": str(year),
        }

    rows = []
    for exp in expenses:
        rows.append([
            exp['date'],
            exp.get('establishment', ''),
            exp.get('category', 'Outros'),
            exp['amount'],
            exp['description'],
        ])

    rows_json = json.dumps(rows)
    result = _run_gog([
        "sheets", "table", "append",
        spreadsheet_id, table_id,
        f"--values-json={rows_json}",
    ])

    if result["success"]:
        return {
            "success": True,
            "rows_added": len(rows),
            "total_amount": sum(e['amount'] for e in expenses),
            "table": table_name,
            "spreadsheet": str(year),
        }
    else:
        return {"success": False, "error": result.get("error", "Failed to append rows")}
