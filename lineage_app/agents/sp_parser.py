"""Pure regex/sqlparse parser for stored procedure lineage extraction.

Hardened version with:
- SQL preprocessing (strip comments, DECLARE, SET, EXEC, TRY/CATCH wrappers)
- Statement segmentation and primary-statement selection
- Multi-word bracketed identifier support ([Backup Date], [RTT Start Date], etc.)
- Proper column expression parsing for aliased bracketed columns
- Improved WHERE-clause splitting into individual conditions
- Join left-table resolution
- Procedure name extraction
"""

import logging
import re
from typing import Dict, List, Any, Optional

import sqlparse
from sqlparse.tokens import Keyword, DML

from . import normalise_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1.4 Shared identifier regex fragments
# ---------------------------------------------------------------------------

# A SQL Server identifier: either a bracketed multi-word name like
# [Backup Date] or [Trust Referral Received Date], a double-quoted
# multi-word name, or a plain \w+ identifier.
IDENTIFIER = r'(?:\[[^\]]+\]|"[^"]+"|\w+)'

# A possibly-qualified identifier: db.schema.table, schema.table, or table,
# where EACH part can be a bracketed/quoted multi-word name.
QUALIFIED_IDENTIFIER = rf'{IDENTIFIER}(?:\s*\.\s*{IDENTIFIER}){{0,2}}'

# Alias-dot-identifier pattern for column expressions like ta.[RTT Start Date]
ALIAS_COLUMN_PATTERN = re.compile(
    rf'^({IDENTIFIER})\s*\.\s*({IDENTIFIER})$'
)

# AS alias pattern supporting bracketed multi-word aliases like AS [Team]
AS_ALIAS_PATTERN = re.compile(
    rf'\s+AS\s+({IDENTIFIER})\s*$', re.IGNORECASE
)


# ---------------------------------------------------------------------------
# 1.1 Pre-processing: strip noise before any pattern matching
# ---------------------------------------------------------------------------

def _preprocess_sql(sp_code: str) -> str:
    """
    Strip T-SQL noise that confuses statement-level parsing:
      - block comments (/* ... */) and line comments (-- ...)
      - GO batch separators
      - SET, USE, DECLARE statements
      - BEGIN TRY / END TRY / BEGIN CATCH / END CATCH wrapper keywords
      - EXEC / EXECUTE calls to other stored procedures
    Returns the cleaned SQL, preserving line breaks.
    """
    result = sp_code

    # 1. Strip block comments (non-greedy, re.DOTALL)
    result = re.sub(r'/\*.*?\*/', '', result, flags=re.DOTALL)

    # 2. Strip line comments (-- to end of line)
    result = re.sub(r'--[^\n]*', '', result)

    # 3. Remove GO batch separators (on their own line)
    result = re.sub(r'^\s*GO\s*$', '', result, flags=re.MULTILINE | re.IGNORECASE)

    # 4. Remove DECLARE statements - conservative: stop at next recognised
    #    top-level keyword or semicolon
    result = re.sub(
        r'(?:^\s*|\n\s*)DECLARE\s+@\w+.*?(?=;|\bDECLARE\b|\bSET\b|\bBEGIN\b'
        r'|\bSELECT\b|\bINSERT\b|\bDELETE\b|\bUPDATE\b|\bMERGE\b|\bEXEC\b)',
        '', result, flags=re.IGNORECASE | re.DOTALL
    )
    # Clean up any trailing semicolons left from DECLARE removal
    result = re.sub(r'^\s*;\s*$', '', result, flags=re.MULTILINE)

    # 5. Remove SET session-option lines (e.g. SET ANSI_NULLS ON, SET DATEFORMAT DMY)
    result = re.sub(
        r'^\s*SET\s+\w+\s+\w+\s*;?\s*$',
        '', result, flags=re.MULTILINE | re.IGNORECASE
    )

    # 6. Remove USE statements
    result = re.sub(
        r'\bUSE\s+' + QUALIFIED_IDENTIFIER + r'\s*;?\s*',
        '', result, flags=re.IGNORECASE
    )

    # 7. Remove EXEC/EXECUTE calls (up to their semicolon or end of line)
    result = re.sub(
        r'\bEXEC(?:UTE)?\s+.*?(?=;|\n)',
        '', result, flags=re.IGNORECASE
    )
    # Clean up any trailing semicolons left from EXEC removal
    result = re.sub(r'^\s*;\s*$', '', result, flags=re.MULTILINE)

    # 8. Remove BEGIN TRY / END TRY / BEGIN CATCH / END CATCH wrapper keywords
    #    (keep their contents, just strip the wrapper keywords)
    result = re.sub(r'\bBEGIN\s+TRY\b', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\bEND\s+TRY\b', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\bBEGIN\s+CATCH\b', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\bEND\s+CATCH\b', '', result, flags=re.IGNORECASE)

    # Clean up excessive blank lines (collapse multiple blank lines to one)
    result = re.sub(r'\n\s*\n\s*\n', '\n\n', result)

    return result


# ---------------------------------------------------------------------------
# 1.2 Statement segmentation
# ---------------------------------------------------------------------------

def _segment_dml_statements(cleaned_sql: str) -> List[Dict[str, Any]]:
    """
    Split the cleaned SQL into a list of top-level DML statements.
    Each statement is a dict:
      {
        "type": "INSERT" | "UPDATE" | "DELETE" | "MERGE" | "SELECT_INTO",
        "text": "<the full statement text>",
        "start_pos": int,
        "has_select": bool,
      }
    Statements are returned in the order they appear in the source.
    """
    # Find all top-level DML keyword starts
    dml_pattern = re.compile(
        r'\b(INSERT\s+INTO|UPDATE|DELETE\s+(?:FROM\s+)?|MERGE(?:\s+INTO)?|'
        r'SELECT\s+.+?\s+INTO)\b',
        re.IGNORECASE
    )

    matches = list(dml_pattern.finditer(cleaned_sql))
    if not matches:
        return []

    statements = []
    for i, match in enumerate(matches):
        start_pos = match.start()
        # Statement text runs from this match to the next match or end of string,
        # but we need to properly track semicolons at depth 0
        if i + 1 < len(matches):
            end_limit = matches[i + 1].start()
        else:
            end_limit = len(cleaned_sql)

        # Scan forward from start_pos to find the real end of this statement
        stmt_end = _find_statement_end(cleaned_sql, start_pos, end_limit)
        stmt_text = cleaned_sql[start_pos:stmt_end].strip()

        # Determine statement type
        keyword_upper = match.group(1).upper().strip()
        if keyword_upper.startswith('INSERT'):
            stmt_type = 'INSERT'
        elif keyword_upper.startswith('UPDATE'):
            stmt_type = 'UPDATE'
        elif keyword_upper.startswith('DELETE'):
            stmt_type = 'DELETE'
        elif keyword_upper.startswith('MERGE'):
            stmt_type = 'MERGE'
        elif 'INTO' in keyword_upper:
            stmt_type = 'SELECT_INTO'
        else:
            stmt_type = 'UNKNOWN'

        # Check if this statement contains a SELECT at depth 0
        has_select = _statement_has_select(stmt_text)

        statements.append({
            "type": stmt_type,
            "text": stmt_text,
            "start_pos": start_pos,
            "has_select": has_select,
        })

    return statements


def _find_statement_end(text: str, start: int, limit: int) -> int:
    """Find the end of a statement by scanning for semicolons at depth 0."""
    depth = 0
    in_string = False
    string_char = None

    i = start
    while i < limit:
        ch = text[i]

        if ch in ("'", '"') and not in_string:
            in_string = True
            string_char = ch
        elif ch in ("'", '"') and in_string and ch == string_char:
            in_string = False
        elif in_string:
            pass
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ';' and depth == 0:
            return i + 1  # include the semicolon

        i += 1

    return limit


def _statement_has_select(stmt_text: str) -> bool:
    """Check if a statement contains a SELECT keyword at depth 0."""
    depth = 0
    in_string = False
    string_char = None
    i = 0

    while i < len(stmt_text):
        ch = stmt_text[i]

        if ch in ("'", '"') and not in_string:
            in_string = True
            string_char = ch
        elif ch in ("'", '"') and in_string and ch == string_char:
            in_string = False
        elif in_string:
            pass
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0:
            # Check for SELECT keyword at depth 0
            rest = stmt_text[i:]
            # Must be a word boundary before SELECT
            if i == 0 or not stmt_text[i - 1].isalnum() and stmt_text[i - 1] != '_':
                if re.match(r'SELECT\b', rest, re.IGNORECASE):
                    return True

        i += 1

    return False


# ---------------------------------------------------------------------------
# 1.2 Choose primary statement
# ---------------------------------------------------------------------------

def _choose_primary_statement(statements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    From all segmented DML statements, choose the one that should drive
    lineage extraction:
      1. Prefer statements with has_select == True
      2. Among those, prefer INSERT/MERGE/SELECT_INTO over UPDATE
      3. If still tied, choose the one with the LONGEST text
      4. Return None if no statement has has_select == True
    """
    candidates = [s for s in statements if s["has_select"]]
    if not candidates:
        return None

    # Prefer INSERT/MERGE/SELECT_INTO over UPDATE
    preferred = [s for s in candidates if s["type"] in ("INSERT", "MERGE", "SELECT_INTO")]
    if preferred:
        candidates = preferred

    # Among remaining, choose the longest
    return max(candidates, key=lambda s: len(s["text"]))


# ---------------------------------------------------------------------------
# 1.3 Procedure name extraction
# ---------------------------------------------------------------------------

def _find_procedure_name(sp_code: str) -> str:
    """
    Extract the procedure name from CREATE/ALTER PROCEDURE statements,
    operating on the ORIGINAL (non-preprocessed) sp_code.
    Returns the LAST segment only (the bare procedure name), normalised.
    """
    pattern = re.compile(
        r'(?:CREATE|ALTER)\s+PROC(?:EDURE)?\s+'
        + QUALIFIED_IDENTIFIER,
        re.IGNORECASE
    )
    match = pattern.search(sp_code)
    if not match:
        return ""
    # Get the last segment of the matched identifier
    full_name = match.group(0)
    # Extract just the identifier part after PROC/PROCEDURE
    proc_part = re.sub(
        r'(?:CREATE|ALTER)\s+PROC(?:EDURE)?\s+',
        '', full_name, flags=re.IGNORECASE
    ).strip()
    return normalise_table(proc_part)


# ---------------------------------------------------------------------------
# Main extraction entry point (rewritten per §1.3)
# ---------------------------------------------------------------------------

def extract_lineage(sp_code: str, catalogue: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Attempt to extract lineage from *sp_code* using regex and sqlparse only.
    Returns a lineage dict on success, or None if extraction is incomplete.
    """
    if not sp_code or not sp_code.strip():
        return None

    cleaned = _preprocess_sql(sp_code)
    statements = _segment_dml_statements(cleaned)
    primary = _choose_primary_statement(statements)
    if primary is None:
        logger.debug("Step 1 failed: no SELECT-bearing DML statement found")
        return None

    stmt_text = primary["text"]

    target_table = _find_target_table(stmt_text)
    if not target_table:
        logger.debug("Step 1 failed: no target table found in primary statement")
        return None

    source_tables = _find_source_tables(stmt_text, target_table)
    if not source_tables:
        logger.debug("Step 1 failed: no source tables found in primary statement")
        return None

    column_mappings = _parse_column_list(stmt_text, source_tables)
    if not column_mappings:
        logger.debug("Step 1 failed: no column mappings found in primary statement")
        return None

    joins = _parse_joins(stmt_text)
    joins = _resolve_join_left_tables(joins, stmt_text, target_table)
    filters = _parse_filters(stmt_text)
    grouping = _parse_grouping(stmt_text)

    return {
        "procedure_name": _find_procedure_name(sp_code),
        "target_table": target_table,
        "source_tables": source_tables,
        "column_mappings": column_mappings,
        "joins": joins,
        "filters": filters,
        "grouping": grouping,
    }


# ---------------------------------------------------------------------------
# Target table, source tables, alias map (updated for QUALIFIED_IDENTIFIER)
# ---------------------------------------------------------------------------

def _find_target_table(sp_code: str) -> Optional[str]:
    """Find the target table from INSERT INTO, SELECT INTO, UPDATE, or MERGE."""
    patterns = [
        (r'INSERT\s+INTO\s+(' + QUALIFIED_IDENTIFIER + r')', re.IGNORECASE),
        (r'SELECT\s+.+?\s+INTO\s+(' + QUALIFIED_IDENTIFIER + r')', re.IGNORECASE),
        (r'UPDATE\s+(' + QUALIFIED_IDENTIFIER + r')', re.IGNORECASE),
        (r'MERGE\s+(?:INTO\s+)?(' + QUALIFIED_IDENTIFIER + r')', re.IGNORECASE),
    ]
    for pattern, flags in patterns:
        match = re.search(pattern, sp_code, flags)
        if match:
            return normalise_table(match.group(1))
    return None


def _find_source_tables(sp_code: str, target_table: str) -> List[str]:
    """Find all source tables from FROM and JOIN clauses, excluding the target."""
    from_pattern = r'\bFROM\s+(' + QUALIFIED_IDENTIFIER + r')'
    join_pattern = r'\bJOIN\s+(' + QUALIFIED_IDENTIFIER + r')'
    source_tables = set()
    for pattern in [from_pattern, join_pattern]:
        for match in re.finditer(pattern, sp_code, re.IGNORECASE):
            norm = normalise_table(match.group(1))
            if norm and norm != target_table:
                source_tables.add(norm)
    return list(source_tables)


def _build_alias_map(sp_code: str) -> Dict[str, str]:
    """Build a mapping from alias to real table name. Keys are lowercase."""
    alias_map = {}
    # The table reference uses QUALIFIED_IDENTIFIER; the alias is plain \w+
    pattern = re.compile(
        r'(?:FROM|JOIN)\s+(' + QUALIFIED_IDENTIFIER + r')\s+(?:AS\s+)?(\w+)',
        re.IGNORECASE
    )
    for match in pattern.finditer(sp_code):
        norm_table = normalise_table(match.group(1))
        if norm_table:
            alias_map[match.group(2).lower()] = norm_table
    return alias_map


# ---------------------------------------------------------------------------
# Column list parsing (updated for bracketed identifiers)
# ---------------------------------------------------------------------------

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


def _parse_column_expression(expr: str, alias_map: Dict[str, str], source_tables: List[str]) -> tuple:
    """Parse a single column expression from the SELECT list."""
    expr = expr.strip()
    if not expr:
        return (None, None, None, None, None)

    # Check for AS alias (supports bracketed multi-word aliases)
    as_match = AS_ALIAS_PATTERN.search(expr)
    if as_match:
        target_col = normalise_table(as_match.group(1))
        expr_without_as = expr[:as_match.start()].strip()
    else:
        target_col = ""
        expr_without_as = expr

    if _is_constant(expr_without_as):
        return (target_col or expr_without_as, "", expr_without_as, "constant", f"Constant value: {expr_without_as}")

    # Try alias.column pattern using regex (handles bracketed columns like ta.[RTT Start Date])
    alias_col_match = ALIAS_COLUMN_PATTERN.match(expr_without_as)
    if alias_col_match:
        table_alias_part = alias_col_match.group(1)
        col_part = alias_col_match.group(2)

        norm_table_alias = normalise_table(table_alias_part)
        if norm_table_alias in source_tables:
            source_table = norm_table_alias
        else:
            # Resolve alias: strip brackets from alias, lowercase for lookup
            possible_alias = table_alias_part.strip('[]').strip('"').lower()
            source_table = alias_map.get(possible_alias, normalise_table(table_alias_part))

        source_col = normalise_table(col_part) if col_part else ""
        trans_type, trans_logic = _classify_transformation(expr_without_as)
        return (target_col or source_col, source_table, source_col, trans_type, trans_logic)

    # Not a simple qualified reference — try to find a table reference in the expression
    source_table = ""
    for m in re.finditer(r'(' + QUALIFIED_IDENTIFIER + r')', expr_without_as):
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
    # CASE...WHEN check FIRST (before aggregation/calculation checks)
    if 'CASE' in expr_upper and 'WHEN' in expr_upper:
        return ("conditional", expr)
    agg_patterns = [r'SUM\s*\(', r'COUNT\s*\(', r'AVG\s*\(', r'MAX\s*\(', r'MIN\s*\(', r'STDEV\s*\(', r'STRING_AGG\s*\(']
    for pattern in agg_patterns:
        if re.search(pattern, expr_upper):
            return ("aggregation", expr)
    calc_patterns = [
        r'CONCAT\s*\(', r'DATEDIFF\s*\(', r'DATEADD\s*\(', r'CAST\s*\(',
        r'CONVERT\s*\(', r'COALESCE\s*\(', r'ISNULL\s*\(', r'FLOOR\s*\(',
        r'CEILING\s*\(', r'ROUND\s*\(', r'LEFT\s*\(', r'RIGHT\s*\(',
        r'SUBSTRING\s*\(', r'REPLACE\s*\(', r'UPPER\s*\(', r'LOWER\s*\(',
    ]
    stripped = re.sub(r'\b\w+\.', '', expr_upper)
    if re.search(r'[+\-*/]', stripped):
        return ("calculation", expr)
    for pattern in calc_patterns:
        if re.search(pattern, expr_upper):
            return ("calculation", expr)
    if re.match(r'^\[?\"?[\w ]+\"?\]?$', expr):
        return ("direct_copy", expr)
    return ("calculation", expr)


# ---------------------------------------------------------------------------
# Joins (updated for QUALIFIED_IDENTIFIER + left-table resolution)
# ---------------------------------------------------------------------------

def _parse_joins(sp_code: str) -> List[Dict[str, str]]:
    """Parse JOIN clauses from the stored procedure."""
    joins = []
    # T-SQL pattern: JOIN <table> [alias] [WITH (hint)] ON
    pattern = re.compile(
        r'(INNER|LEFT|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?JOIN\s+('
        + QUALIFIED_IDENTIFIER
        + r')\s+(?:\w+\s+)?(?:WITH\s*\([^)]*\)\s+)?ON\s+',
        re.IGNORECASE
    )
    for match in pattern.finditer(sp_code):
        join_type = match.group(1).strip().upper()
        right_table = normalise_table(match.group(2))
        on_start = match.end()
        condition = _extract_until_keyword(sp_code[on_start:])
        joins.append({"left_table": "", "right_table": right_table, "join_type": join_type, "condition": condition})
    return joins


def _resolve_join_left_tables(
    joins: List[Dict[str, str]], stmt_text: str, target_table: str
) -> List[Dict[str, str]]:
    """
    For each join dict, set left_table to the first FROM-clause table.
    T-SQL joins chain left-to-right against the FROM clause, so this is
    a reasonable simplification for display purposes.
    """
    if not joins:
        return joins

    from_pattern = re.compile(r'\bFROM\s+(' + QUALIFIED_IDENTIFIER + r')', re.IGNORECASE)
    from_match = from_pattern.search(stmt_text)
    if from_match:
        left_table = normalise_table(from_match.group(1))
        for join in joins:
            join["left_table"] = left_table

    return joins


# ---------------------------------------------------------------------------
# Filters (updated with proper WHERE-clause splitting)
# ---------------------------------------------------------------------------

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
            for kw in ('WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT', 'OFFSET',
                        'INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS', 'END'):
                if rest.startswith(kw):
                    return text[:i].strip()
    return text.strip()


def _split_by_top_level_and_or(s: str) -> List[str]:
    """
    Split a WHERE-clause string into individual condition strings on
    top-level AND/OR boundaries (not inside parentheses or string literals).
    Returns the list of trimmed condition strings.
    """
    parts = []
    current = []
    depth = 0
    in_string = False
    string_char = None
    i = 0

    while i < len(s):
        ch = s[i]

        if ch in ("'", '"') and not in_string:
            in_string = True
            string_char = ch
            current.append(ch)
            i += 1
        elif ch in ("'", '"') and in_string and ch == string_char:
            in_string = False
            current.append(ch)
            i += 1
        elif in_string:
            current.append(ch)
            i += 1
        elif ch == '(':
            depth += 1
            current.append(ch)
            i += 1
        elif ch == ')':
            depth -= 1
            current.append(ch)
            i += 1
        elif depth == 0:
            # Check for top-level AND/OR
            rest = s[i:]
            and_match = re.match(r'\s*\bAND\b\s+', rest, re.IGNORECASE)
            or_match = re.match(r'\s*\bOR\b\s+', rest, re.IGNORECASE)
            if and_match:
                parts.append(''.join(current).strip())
                current = []
                i += and_match.end()
            elif or_match:
                parts.append(''.join(current).strip())
                current = []
                i += or_match.end()
            else:
                current.append(ch)
                i += 1
        else:
            current.append(ch)
            i += 1

    if current:
        part = ''.join(current).strip()
        if part:
            parts.append(part)
    return parts


def _parse_filters(sp_code: str) -> List[str]:
    """Extract conditions from the WHERE clause, split into individual conditions."""
    where_match = re.search(r'\bWHERE\b\s+', sp_code, re.IGNORECASE)
    if not where_match:
        return []

    # Extract the WHERE clause content using _extract_until_keyword logic
    where_start = where_match.end()
    where_clause = _extract_until_keyword(sp_code[where_start:])

    if not where_clause:
        return []

    # Split into individual conditions
    conditions = _split_by_top_level_and_or(where_clause)
    return [c for c in conditions if c]


def _parse_grouping(sp_code: str) -> List[str]:
    """Extract expressions from the GROUP BY clause."""
    group_match = re.search(r'GROUP\s+BY\s+(.*)', sp_code, re.IGNORECASE | re.DOTALL)
    if not group_match:
        return []
    group_clause = group_match.group(1)
    # Use _extract_until_keyword to properly bound the clause
    group_clause = _extract_until_keyword(group_clause)
    return _split_by_top_level_comma(group_clause)


# ---------------------------------------------------------------------------
# 1.10 Unit tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.DEBUG)

    # ---- Test 3: existing simple sp_example.sql case ----
    sp_example = """
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

    result = extract_lineage(sp_example, catalogue)
    print("\n=== Test 3: simple sp_example ===")
    print(f"target_table: {result['target_table']}")
    print(f"source_tables: {result['source_tables']}")
    print(f"column_mappings count: {len(result['column_mappings'])}")
    assert result["target_table"] == "CUSTOMER_SUMMARY", f"Expected CUSTOMER_SUMMARY, got {result['target_table']}"
    assert "CUSTOMERS" in result["source_tables"], "CUSTOMERS should be in source_tables"
    assert "ORDERS" in result["source_tables"], "ORDERS should be in source_tables"
    assert len(result["column_mappings"]) >= 3, f"Expected >= 3 column mappings, got {len(result['column_mappings'])}"
    print("PASSED")

    # ---- Test 2: multi-word bracketed alias case ----
    sp_bracketed = """
    CREATE PROCEDURE dbo.sp_test
    AS
    BEGIN
        INSERT INTO [dbo].[Target] ([Trust Referral Received Date])
        SELECT ta.[RTT Start Date] AS [Trust Referral Received Date]
        FROM dbo.A ta
    END
    """
    catalogue2 = {
        "A": {"qualified_name": "dbo.A", "database": "DB", "schema": "DBO", "columns": [{"name": "RTT START DATE", "type": "DATE", "nullable": True, "primary_key": False}], "source_file": "tables.sql"},
        "TARGET": {"qualified_name": "[dbo].[Target]", "database": "DB", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
    }
    result2 = extract_lineage(sp_bracketed, catalogue2)
    print("\n=== Test 2: bracketed alias case ===")
    print(f"target_table: {result2['target_table']}")
    print(f"column_mappings: {result2['column_mappings']}")
    if result2["column_mappings"]:
        mapping = result2["column_mappings"][0]
        print(f"  source_column: {mapping['source_column']}")
        print(f"  target_column: {mapping['target_column']}")
        assert "RTT" in mapping["source_column"].upper(), f"Expected RTT in source_column, got {mapping['source_column']}"
    print("PASSED")

    # ---- Test 4: DELETE-then-INSERT case ----
    sp_delete_insert = """
    CREATE PROCEDURE dbo.sp_multi
    AS
    BEGIN
        DELETE FROM [dbo].[Target] WHERE id = 1;
        DELETE FROM [dbo].[Target] WHERE id = 2;
        INSERT INTO [dbo].[Target] (col1, col2)
        SELECT a.col1, a.col2 FROM [dbo].[Source] a;
    END
    """
    catalogue3 = {
        "SOURCE": {"qualified_name": "[dbo].[Source]", "database": "DB", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
        "TARGET": {"qualified_name": "[dbo].[Target]", "database": "DB", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
    }
    result3 = extract_lineage(sp_delete_insert, catalogue3)
    print("\n=== Test 4: DELETE-then-INSERT ===")
    print(f"target_table: {result3['target_table']}")
    print(f"source_tables: {result3['source_tables']}")
    assert result3["target_table"] == "TARGET", f"Expected TARGET, got {result3['target_table']}"
    assert "SOURCE" in result3["source_tables"], "SOURCE should be in source_tables"
    print("PASSED")

    # ---- Test 1: Vault.TrustAccessWaits (real-world complex SP) ----
    vault_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'sp', 'Vault.TrustAccessWaits.sql')
    if os.path.exists(vault_path):
        with open(vault_path, 'r', encoding='utf-8', errors='ignore') as f:
            vault_code = f.read()

        vault_catalogue = {
            "WAITINGTIMES": {"qualified_name": "[Vault].[WaitingTimes]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "PATIENT": {"qualified_name": "[Vault].[Patient]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "EPISODE": {"qualified_name": "[Vault].[Episode]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "TRUSTACCESSWAITINGTIMES": {"qualified_name": "[Vault].[TrustAccessWaitingTimes]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "DIMSD": {"qualified_name": "[Vault].[DimSD]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "PRIORITY": {"qualified_name": "[Dim].[Priority]", "database": "DIM", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "REFERRALSTATUSVALUES": {"qualified_name": "[Vault].[ReferralStatusValues]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
            "TRUSTACCESSWAITS": {"qualified_name": "[Vault].[TrustAccessWaits]", "database": "VAULT", "schema": "DBO", "columns": [], "source_file": "tables.sql"},
        }

        result1 = extract_lineage(vault_code, vault_catalogue)
        print("\n=== Test 1: Vault.TrustAccessWaits ===")
        if result1 is None:
            print("FAILED: extraction returned None")
        else:
            print(f"target_table: {result1['target_table']}")
            print(f"source_tables: {result1['source_tables']}")
            print(f"column_mappings count: {len(result1['column_mappings'])}")
            print(f"filters: {result1['filters']}")
            print(f"joins count: {len(result1['joins'])}")

            assert result1["target_table"] == "TRUSTACCESSWAITS", f"Expected TRUSTACCESSWAITS, got {result1['target_table']}"
            assert "SSMS" not in result1["source_tables"], f"SSMS should NOT be in source_tables: {result1['source_tables']}"

            expected_sources = ["WAITINGTIMES", "PATIENT", "EPISODE", "TRUSTACCESSWAITINGTIMES", "DIMSD", "PRIORITY", "REFERRALSTATUSVALUES"]
            for src in expected_sources:
                assert src in result1["source_tables"], f"{src} should be in source_tables: {result1['source_tables']}"

            assert len(result1["column_mappings"]) >= 20, f"Expected >= 20 column mappings, got {len(result1['column_mappings'])}"

            for mapping in result1["column_mappings"]:
                assert "@" not in mapping["target_column"], f"target_column should not contain @: {mapping['target_column']}"
                assert "COUNT(1)" not in mapping["target_column"], f"target_column should not contain COUNT(1): {mapping['target_column']}"

            has_conditional = any(m["transformation_type"] == "conditional" for m in result1["column_mappings"])
            assert has_conditional, "At least one mapping should have transformation_type == 'conditional'"

            has_floor = any("FLOOR" in m.get("transformation_logic", "").upper() or "floor" in m.get("transformation_logic", "") for m in result1["column_mappings"])
            assert has_floor, "At least one mapping should contain FLOOR in transformation_logic"

            assert len(result1["filters"]) > 1, f"Expected > 1 filter condition, got {len(result1['filters'])}"
            for f in result1["filters"]:
                assert len(f) <= 200, f"Filter condition too long ({len(f)} chars): {f[:80]}..."

            for join in result1["joins"]:
                assert join["left_table"], f"left_table should not be empty for join: {join}"

            print("PASSED")
    else:
        print(f"\n=== Test 1: SKIPPED (file not found: {vault_path}) ===")

    print("\nAll tests completed.")
