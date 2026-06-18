"""Pure regex/sqlparse parser for stored procedure lineage extraction."""

import logging
import re
from typing import Dict, List, Any, Optional

import sqlparse
from sqlparse.tokens import Keyword, DML

from . import normalise_table

logger = logging.getLogger(__name__)


def extract_lineage(sp_code: str, catalogue: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Attempt to extract lineage from *sp_code* using regex and sqlparse only.
    Returns a lineage dict on success, or None if extraction is incomplete.
    """
    if not sp_code or not sp_code.strip():
        return None

    target_table = _find_target_table(sp_code)
    if not target_table:
        logger.debug("Step 1 failed: No target table found")
        return None

    source_tables = _find_source_tables(sp_code, target_table)
    if not source_tables:
        logger.debug("Step 1 failed: No source tables found")
        return None

    column_mappings = _parse_column_list(sp_code, source_tables)
    if not column_mappings:
        logger.debug("Step 1 failed: No column mappings found")
        return None

    joins = _parse_joins(sp_code)
    filters = _parse_filters(sp_code)
    grouping = _parse_grouping(sp_code)

    return {
        "procedure_name": "",
        "target_table": target_table,
        "source_tables": source_tables,
        "column_mappings": column_mappings,
        "joins": joins,
        "filters": filters,
        "grouping": grouping,
    }


def _find_target_table(sp_code: str) -> Optional[str]:
    """Find the target table from INSERT INTO, SELECT INTO, UPDATE, or MERGE."""
    patterns = [
        (r'INSERT\s+INTO\s+([\[\]"\w\.]+)', re.IGNORECASE),
        (r'SELECT\s+.+?\s+INTO\s+([\[\]"\w\.]+)', re.IGNORECASE),
        (r'UPDATE\s+([\[\]"\w\.]+)', re.IGNORECASE),
        (r'MERGE\s+(?:INTO\s+)?([\[\]"\w\.]+)', re.IGNORECASE),
    ]
    for pattern, flags in patterns:
        match = re.search(pattern, sp_code, flags)
        if match:
            return normalise_table(match.group(1))
    return None


def _find_source_tables(sp_code: str, target_table: str) -> List[str]:
    """Find all source tables from FROM and JOIN clauses, excluding the target."""
    patterns = [r'FROM\s+([\[\]"\w\.]+)', r'JOIN\s+([\[\]"\w\.]+)']
    source_tables = set()
    for pattern in patterns:
        for match in re.finditer(pattern, sp_code, re.IGNORECASE):
            norm = normalise_table(match.group(1))
            if norm and norm != target_table:
                source_tables.add(norm)
    return list(source_tables)


def _split_by_top_level_comma(s: str) -> List[str]:
    """Split a string by commas that are not inside parentheses or strings."""
    parts = []
    current = []
    depth = 0
    in_string = False
    string_char = None
    for ch in s:
        if ch in ("'", '"') and not in_string:
            in_string = True
            string_char = ch
            current.append(ch)
        elif ch in ("'", '"') and in_string and ch == string_char:
            in_string = False
            current.append(ch)
        elif in_string:
            current.append(ch)
        elif ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return parts


def _parse_column_list(sp_code: str, source_tables: List[str]) -> List[Dict[str, Any]]:
    """Parse the SELECT column list to build column mappings."""
    select_pattern = re.compile(r'\bSELECT\b\s+(.*?)\s+\bFROM\b', re.IGNORECASE | re.DOTALL)
    match = select_pattern.search(sp_code)
    if not match:
        logger.debug("No SELECT...FROM block found")
        return []

    col_list_str = match.group(1)
    col_expressions = _split_by_top_level_comma(col_list_str)
    alias_map = _build_alias_map(sp_code)

    mappings = []
    for expr in col_expressions:
        if not expr:
            continue
        target_col, source_table, source_col, trans_type, trans_logic = _parse_column_expression(expr, alias_map, source_tables)
        if target_col is None:
            continue
        mappings.append({
            "target_column": target_col,
            "source_table": source_table,
            "source_column": source_col,
            "transformation_type": trans_type,
            "transformation_logic": trans_logic,
        })
    return mappings


def _build_alias_map(sp_code: str) -> Dict[str, str]:
    """Build a mapping from alias to real table name. Keys are lowercase."""
    alias_map = {}
    pattern = re.compile(r'(?:FROM|JOIN)\s+([\[\]"\w\.]+)\s+(?:AS\s+)?(\w+)', re.IGNORECASE)
    for match in pattern.finditer(sp_code):
        norm_table = normalise_table(match.group(1))
        if norm_table:
            alias_map[match.group(2).lower()] = norm_table
    return alias_map


def _parse_column_expression(expr: str, alias_map: Dict[str, str], source_tables: List[str]) -> tuple:
    """Parse a single column expression from the SELECT list."""
    expr = expr.strip()
    if not expr:
        return (None, None, None, None, None)

    as_match = re.search(r'\s+AS\s+(\w+)$', expr, re.IGNORECASE)
    if as_match:
        target_col = normalise_table(as_match.group(1))
        expr_without_as = expr[:as_match.start()].strip()
    else:
        target_col = ""
        expr_without_as = expr

    if _is_constant(expr_without_as):
        return (target_col or expr_without_as, "", expr_without_as, "constant", f"Constant value: {expr_without_as}")

    parts = expr_without_as.split('.')
    if len(parts) >= 2:
        col_part = parts[-1]
        table_alias_part = '.'.join(parts[:-1])
        norm_table_alias = normalise_table(table_alias_part)
        if norm_table_alias in source_tables:
            source_table = norm_table_alias
        else:
            possible_alias = table_alias_part.split('.')[-1].strip('[]').strip('"').lower()
            source_table = alias_map.get(possible_alias, normalise_table(table_alias_part))
        source_col = normalise_table(col_part) if col_part else ""
        trans_type, trans_logic = _classify_transformation(expr_without_as)
        return (target_col or source_col, source_table, source_col, trans_type, trans_logic)

    # Not a qualified reference — try to find a table reference in the expression
    source_table = ""
    for m in re.finditer(r'([\[\]"\w\.]+)', expr_without_as):
        norm = normalise_table(m.group(1))
        if norm in source_tables:
            source_table = norm
            break
    source_col = expr_without_as
    trans_type, trans_logic = _classify_transformation(expr_without_as)
    return (target_col or source_col, source_table, source_col, trans_type, trans_logic)


def _is_constant(expr: str) -> bool:
    """Check if the expression is a constant value."""
    expr = expr.strip()
    if expr.startswith(("'", '"')) and expr.endswith(("'", '"')) and len(expr) >= 2:
        return True
    if re.match(r'^\d+(\.\d+)?$', expr):
        return True
    if expr.upper() in ('GETDATE()', 'CURRENT_TIMESTAMP', 'SYSDATETIME()'):
        return True
    return False


def _classify_transformation(expr: str) -> tuple[str, str]:
    """Classify the transformation type and return a one-line description."""
    expr_upper = expr.upper()
    if 'CASE' in expr_upper and 'WHEN' in expr_upper:
        return ("conditional", expr)
    agg_patterns = [r'SUM\s*\(', r'COUNT\s*\(', r'AVG\s*\(', r'MAX\s*\(', r'MIN\s*\(', r'STDEV\s*\(', r'STRING_AGG\s*\(']
    for pattern in agg_patterns:
        if re.search(pattern, expr_upper):
            return ("aggregation", expr)
    calc_patterns = [r'CONCAT\s*\(', r'DATEDIFF\s*\(', r'DATEADD\s*\(', r'CAST\s*\(', r'CONVERT\s*\(', r'COALESCE\s*\(', r'ISNULL\s*\(']
    stripped = re.sub(r'\b\w+\.', '', expr_upper)
    if re.search(r'[+\-*/]', stripped):
        return ("calculation", expr)
    for pattern in calc_patterns:
        if re.search(pattern, expr_upper):
            return ("calculation", expr)
    if re.match(r'^\[?\"?[\w]+\"?\]?$', expr):
        return ("direct_copy", expr)
    return ("calculation", expr)


def _parse_joins(sp_code: str) -> List[Dict[str, str]]:
    """Parse JOIN clauses from the stored procedure."""
    joins = []
    pattern = re.compile(
        r'(INNER|LEFT|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?JOIN\s+([\[\]"\w\.]+)\s+(?:AS\s+\w+\s+)?ON\s+',
        re.IGNORECASE | re.VERBOSE
    )
    for match in pattern.finditer(sp_code):
        join_type = match.group(1).strip().upper()
        right_table = normalise_table(match.group(2))
        on_start = match.end()
        condition = _extract_until_keyword(sp_code[on_start:])
        joins.append({"left_table": "", "right_table": right_table, "join_type": join_type, "condition": condition})
    return joins


def _extract_until_keyword(text: str) -> str:
    """Extract text until a terminating SQL keyword at depth 0."""
    depth = 0
    in_string = False
    string_char = None
    for i, ch in enumerate(text):
        if ch in ("'", '"') and not in_string:
            in_string = True
            string_char = ch
        elif ch in ("'", '"') and in_string and ch == string_char:
            in_string = False
        elif in_string:
            continue
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ';' and depth == 0:
            return text[:i].strip()
        elif depth == 0 and ch in (' ', '\n', '\r', '\t'):
            rest = text[i:].lstrip().upper()
            for kw in ('WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT', 'OFFSET', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS'):
                if rest.startswith(kw):
                    return text[:i].strip()
    return text.strip()


def _parse_filters(sp_code: str) -> List[str]:
    """Extract conditions from the WHERE clause."""
    where_match = re.search(r'WHERE\s+(.*)', sp_code, re.IGNORECASE | re.DOTALL)
    if not where_match:
        return []
    where_clause = where_match.group(1)
    for stop_word in ['GROUP BY', 'HAVING', 'ORDER BY', ';', 'END']:
        idx = where_clause.upper().find(stop_word.upper())
        if idx >= 0:
            where_clause = where_clause[:idx]
    where_clause = where_clause.strip()
    if not where_clause:
        return []
    return [where_clause]


def _parse_grouping(sp_code: str) -> List[str]:
    """Extract expressions from the GROUP BY clause."""
    group_match = re.search(r'GROUP\s+BY\s+(.*)', sp_code, re.IGNORECASE | re.DOTALL)
    if not group_match:
        return []
    group_clause = group_match.group(1)
    for stop_word in [';', 'END', 'GO', 'HAVING', 'ORDER BY']:
        idx = group_clause.upper().find(stop_word.upper())
        if idx >= 0:
            group_clause = group_clause[:idx]
    return _split_by_top_level_comma(group_clause)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    sp_code = open("data/sp/sp_example.sql").read() if __import__('os').path.exists("data/sp/sp_example.sql") else """
    CREATE PROCEDURE [SalesDB].[dbo].[sp_load_customer_summary]
    AS
    BEGIN
        INSERT INTO [SalesDB].[dbo].[CUSTOMER_SUMMARY] (customer_id, customer_name, total_orders, total_revenue, last_order_date)
        SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS total_orders, SUM(o.order_amount) AS total_revenue, MAX(o.order_date) AS last_order_date
        FROM [SalesDB].[dbo].[CUSTOMERS] c
        LEFT JOIN [SalesDB].[dbo].[ORDERS] o ON c.customer_id = o.customer_id
        GROUP BY c.customer_id, c.customer_name;
    END
    """
    catalogue = {
        "CUSTOMERS": {"qualified_name": "[SalesDB].[dbo].[Customers]", "database": "SALESDB", "schema": "DBO", "columns": [{"name": "CUSTOMER_ID", "type": "INT", "nullable": False, "primary_key": True}], "source_file": "tables.sql"},
        "ORDERS": {"qualified_name": "[SalesDB].[dbo].[Orders]", "database": "SALESDB", "schema": "DBO", "columns": [{"name": "ORDER_ID", "type": "INT", "nullable": False, "primary_key": True}], "source_file": "tables.sql"},
        "CUSTOMER_SUMMARY": {"qualified_name": "[SalesDB].[dbo].[Customer_Summary]", "database": "SALESDB", "schema": "DBO", "columns": [{"name": "CUSTOMER_ID", "type": "INT", "nullable": False, "primary_key": True}], "source_file": "tables.sql"}
    }
    result = extract_lineage(sp_code, catalogue)
    print(result)