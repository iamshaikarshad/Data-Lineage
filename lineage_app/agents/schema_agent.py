"""SchemaAgent for parsing CREATE TABLE statements and building a table catalogue."""

import logging
import re
from pathlib import Path
from typing import Dict, List, Any, Union, Tuple, Set, Optional, TYPE_CHECKING

from . import normalise_table

if TYPE_CHECKING:
    from .tracker import ProcessingTracker

logger = logging.getLogger(__name__)


class SchemaAgent:
    """
    Reads every .sql file under data/schemas/ (recursively), parses every
    CREATE TABLE statement it finds, and returns a catalogue dictionary.
    """

    def __init__(self, schema_dir: Union[str, Path], tracker: Optional["ProcessingTracker"] = None):
        """
        Initialize the SchemaAgent.

        Args:
            schema_dir: Path to the directory containing schema SQL files.
            tracker: Optional ProcessingTracker to skip already-processed files.
        """
        self.schema_dir = Path(schema_dir)
        self.catalogue: Dict[str, Dict[str, Any]] = {}
        self.tracker = tracker

    def run(self) -> Dict[str, Dict[str, Any]]:
        """
        Parse all schema files and return the catalogue.

        If a ProcessingTracker is attached, already-processed schema files
        are skipped entirely.

        Returns:
            A dictionary mapping normalised table names to their metadata.
        """
        logger.info("Starting SchemaAgent")
        all_schema_files = list(self.schema_dir.rglob("*.sql"))
        logger.info(f"Found {len(all_schema_files)} schema file(s)")

        # Filter out already-processed files when tracker is available
        if self.tracker:
            unprocessed = [f for f in all_schema_files if not self.tracker.is_schema_processed(f.name)]
            skipped = len(all_schema_files) - len(unprocessed)
            if skipped:
                logger.info(
                    f"Tracker: skipping {skipped} already-processed schema(s), "
                    f"processing {len(unprocessed)} new"
                )
                for f in all_schema_files:
                    if self.tracker.is_schema_processed(f.name):
                        logger.info(f"  Skipping already-processed schema: {f.name}")
            schema_files = unprocessed
        else:
            schema_files = all_schema_files

        tables_before = len(self.catalogue)
        for schema_file in schema_files:
            self._parse_file(schema_file)
            # Count tables added from this file
            tables_after_file = len(self.catalogue)
            tables_from_file = tables_after_file - tables_before
            tables_before = tables_after_file

            if self.tracker:
                self.tracker.mark_schema(schema_file.name, "success", tables_found=tables_from_file)

        # Persist tracker state
        if self.tracker:
            self.tracker.save()

        logger.info(f"SchemaAgent: found {len(self.catalogue)} tables")
        return self.catalogue

    def _parse_file(self, file_path: Path) -> None:
        """
        Parse a single schema file for CREATE TABLE statements.

        Args:
            file_path: Path to the SQL file.
        """
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return

        # Split by CREATE TABLE boundary (lookahead to keep the delimiter)
        statements = re.split(r'(?=CREATE\s+TABLE\b)', content, flags=re.IGNORECASE)
        for stmt in statements:
            if not stmt.strip():
                continue
            if not re.match(r'CREATE\s+TABLE\b', stmt, flags=re.IGNORECASE):
                continue
            self._parse_create_table(stmt, file_path.name)

    def _parse_create_table(self, stmt: str, source_file: str) -> None:
        """
        Parse a single CREATE TABLE statement.

        Args:
            stmt: The CREATE TABLE statement string.
            source_file: The name of the file the statement came from.
        """
        # -- 1. Extract the table header (everything between CREATE TABLE and first '(') --
        header_pattern = re.compile(
            r'CREATE\s+TABLE\s+(.+?)\s*\(',
            re.IGNORECASE | re.DOTALL
        )
        header_match = header_pattern.search(stmt)
        if not header_match:
            logger.warning(f"Could not parse table header from: {stmt[:100]}...")
            return

        header = header_match.group(1).strip()
        # Handle optional IF NOT EXISTS / IF EXISTS
        header = re.sub(r'^IF\s+(NOT\s+)?EXISTS\s+', '', header, flags=re.IGNORECASE)

        # Parse dot-separated identifiers (handles brackets, quotes, spaces, special chars)
        identifiers = self._parse_identifiers(header)
        if not identifiers:
            logger.warning(f"Could not parse identifiers from header: {header}")
            return

        # Assign database / schema / table based on number of parts
        # 1 part  → table only
        # 2 parts → schema.table
        # 3+ parts → database.schema.table (take last 3)
        if len(identifiers) == 1:
            db_part, schema_part, table_part = None, None, identifiers[0]
        elif len(identifiers) == 2:
            db_part, schema_part, table_part = None, identifiers[0], identifiers[1]
        else:
            db_part, schema_part, table_part = identifiers[-3], identifiers[-2], identifiers[-1]

        table_name = normalise_table(table_part) if table_part else ""
        if not table_name:
            logger.warning(f"Empty table name after normalisation in: {stmt[:100]}...")
            return

        # Build qualified name preserving original case
        qualified_parts = [p for p in [db_part, schema_part, table_part] if p is not None]
        qualified_name = ".".join(qualified_parts)

        # Normalise database and schema (uppercase) for catalogue fields
        database = normalise_table(db_part) if db_part else ""
        schema = normalise_table(schema_part) if schema_part else ""

        # -- 2. Extract the column body (between first '(' and its matching ')') --
        body_start = header_match.end()  # position right after the '('
        body_end = self._find_matching_paren(stmt, body_start)
        if body_end == -1:
            logger.warning(f"Could not find matching ')' for column body in: {stmt[:100]}...")
            return

        column_body = stmt[body_start:body_end]

        # -- 3. Parse columns and constraints --
        columns, pk_columns = self._parse_columns_and_constraints(column_body)

        # -- 4. Mark PRIMARY KEY columns from constraint definitions --
        # Use case-insensitive lookup since SQL identifiers are case-insensitive
        col_lookup: Dict[str, Dict[str, Any]] = {}
        for col in columns:
            col_lookup[col["name"].lower()] = col
        for pk_col_name in pk_columns:
            col = col_lookup.get(pk_col_name.lower())
            if col:
                col["primary_key"] = True
                col["nullable"] = False

        # -- 5. Store in catalogue --
        self.catalogue[table_name] = {
            "qualified_name": qualified_name,
            "database": database,
            "schema": schema,
            "columns": columns,
            "source_file": source_file
        }

        logger.debug(f"Parsed table: {table_name} from {source_file}")

    # ------------------------------------------------------------------ #
    #  Identifier parsing helpers                                         #
    # ------------------------------------------------------------------ #

    def _parse_identifiers(self, header: str) -> List[str]:
        """
        Parse a dot-separated identifier string like '[DB].[Schema].[Table Name]'
        into a list of clean identifier strings, respecting bracket and quote
        delimiters so that dots inside brackets are not treated as separators.

        Args:
            header: The raw header string (e.g. '[Vault].[DimSD]').

        Returns:
            A list of identifier strings with brackets/quotes removed.
        """
        identifiers: List[str] = []
        current = ""
        in_bracket = False
        in_quote = False
        quote_char: Optional[str] = None

        for ch in header:
            if in_bracket:
                current += ch
                if ch == ']':
                    in_bracket = False
            elif in_quote:
                current += ch
                if ch == quote_char:
                    in_quote = False
            else:
                if ch == '[':
                    in_bracket = True
                    current += ch
                elif ch in ('"', '`'):
                    in_quote = True
                    quote_char = ch
                    current += ch
                elif ch == '.':
                    identifiers.append(self._clean_identifier(current))
                    current = ""
                else:
                    current += ch

        if current.strip():
            identifiers.append(self._clean_identifier(current))

        return identifiers

    @staticmethod
    def _clean_identifier(s: str) -> str:
        """Remove surrounding brackets, double-quotes, or backticks from an identifier."""
        s = s.strip()
        if len(s) >= 2:
            if s[0] == '[' and s[-1] == ']':
                return s[1:-1]
            if s[0] == '"' and s[-1] == '"':
                return s[1:-1]
            if s[0] == '`' and s[-1] == '`':
                return s[1:-1]
        return s

    # ------------------------------------------------------------------ #
    #  Parenthesis / depth tracking helpers                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_matching_paren(text: str, start: int) -> int:
        """
        Given that position ``start`` is just past an opening '(', find the
        index of the matching closing ')' in ``text``.  String literals
        (single and double quotes) are respected so that parentheses inside
        strings are ignored.

        Returns:
            The index of the matching ')', or -1 if not found.
        """
        depth = 1
        in_string = False
        string_char: Optional[str] = None

        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if ch == string_char:
                    in_string = False
                    string_char = None
                continue
            if ch in ("'", '"'):
                in_string = True
                string_char = ch
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
        return -1

    @staticmethod
    def _count_paren_depth_change(s: str) -> int:
        """
        Compute the net change in parenthesis depth for a single line,
        ignoring parentheses inside string literals.

        Returns:
            Net depth change (positive = more opens, negative = more closes).
        """
        depth = 0
        in_string = False
        string_char: Optional[str] = None

        for ch in s:
            if in_string:
                if ch == string_char:
                    in_string = False
                    string_char = None
                continue
            if ch in ("'", '"'):
                in_string = True
                string_char = ch
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        return depth

    # ------------------------------------------------------------------ #
    #  Column / constraint parsing                                        #
    # ------------------------------------------------------------------ #

    def _parse_columns_and_constraints(
        self, column_body: str
    ) -> Tuple[List[Dict[str, Any]], Set[str]]:
        """
        Separate the column body into column-definition lines and
        constraint lines, then parse each.

        Uses a depth tracker so that lines inside a constraint's
        parentheses (e.g. ``[PrioritySK] ASC`` inside a PRIMARY KEY block)
        are NOT mistaken for column definitions.

        Returns:
            A tuple of (list of column dicts, set of PK column names).
        """
        lines = column_body.split('\n')

        column_lines: List[str] = []
        constraint_lines: List[str] = []
        depth = 0
        in_constraint = False

        constraint_keywords = (
            'CONSTRAINT', 'PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY',
            'CHECK', 'INDEX', 'KEY',
        )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip comment lines
            if stripped.startswith('--'):
                continue

            depth_change = self._count_paren_depth_change(stripped)
            upper = stripped.upper()

            # Detect start of a constraint block (only at depth 0)
            is_constraint_start = any(
                upper.startswith(kw) for kw in constraint_keywords
            )
            if is_constraint_start and depth == 0:
                in_constraint = True

            if in_constraint:
                constraint_lines.append(stripped)
            elif depth == 0:
                # Skip stray WITH / ON / closing-paren lines
                if (not upper.startswith('WITH')
                        and not upper.startswith('ON')
                        and not stripped.startswith(')')):
                    column_lines.append(stripped)

            depth += depth_change
            if depth <= 0:
                depth = 0
                in_constraint = False

        # Parse individual column definitions
        columns: List[Dict[str, Any]] = []
        for line in column_lines:
            col = self._parse_column_line(line)
            if col:
                columns.append(col)

        # Extract PRIMARY KEY column names from constraint text
        pk_columns = self._extract_pk_columns(constraint_lines)

        return columns, pk_columns

    def _parse_column_line(self, line: str) -> Optional[Dict[str, Any]]:
        """
        Parse a single column-definition line such as::

            [Team Name] [varchar](255) NOT NULL,
            [PrioritySK] [smallint] IDENTITY(-1,1) NOT NULL,
            [TrustID] [nvarchar](max) NULL,
            [Age] [numeric](18, 0) NULL,

        Returns:
            A dict with keys name, type, nullable, primary_key — or None
            if the line cannot be parsed as a column definition.
        """
        # Remove trailing comma
        line = line.rstrip(',').strip()
        if not line:
            return None

        # Column name: bracketed [..], quoted "..", or bare word
        # Type name:   optional brackets, bare word
        # Type params: optional (..)  — digits, commas, 'max', spaces
        col_pattern = re.compile(
            r'''
            ^\s*
            (?:\[([^\]]+)\]|"([^"]+)"|(\w+))   # column name  (groups 1/2/3)
            \s+
            \[?(\w+)\]?                         # type name    (group 4)
            (?:\s*\(\s*([^)]*?)\s*\))?          # type params  (group 5)
            ''',
            re.VERBOSE
        )

        col_match = col_pattern.match(line)
        if not col_match:
            logger.debug(f"Skipping unrecognised column line: {line}")
            return None

        # Extract column name from whichever alternative matched
        col_name = (col_match.group(1)
                    or col_match.group(2)
                    or col_match.group(3)
                    or "")
        type_base = col_match.group(4) or ""
        type_params = col_match.group(5)  # may be None

        # Skip computed columns (AS …)
        if type_base.upper() == 'AS':
            logger.debug(f"Skipping computed column: {line}")
            return None

        # Build the data-type string, preserving original case
        if type_params is not None:
            data_type = f"{type_base}({type_params})"
        else:
            data_type = type_base

        # Determine nullability and inline PRIMARY KEY
        nullable = True
        primary_key = False

        if re.search(r'\bNOT\s+NULL\b', line, re.IGNORECASE):
            nullable = False
        if re.search(r'\bPRIMARY\s+KEY\b', line, re.IGNORECASE):
            primary_key = True
            nullable = False

        return {
            "name": col_name,
            "type": data_type,
            "nullable": nullable,
            "primary_key": primary_key,
        }

    @staticmethod
    def _extract_pk_columns(constraint_lines: List[str]) -> Set[str]:
        """
        Given the list of constraint lines collected from the column body,
        find every PRIMARY KEY constraint and extract the column names
        referenced in it.

        Handles both single-line and multi-line constraint definitions::

            CONSTRAINT [PK] PRIMARY KEY CLUSTERED
            (
                [Col1] ASC,
                [Col2] ASC
            )WITH (...) ON [PRIMARY]

        Returns:
            A set of column names (original case, brackets removed).
        """
        if not constraint_lines:
            return set()

        constraint_text = ' '.join(constraint_lines)
        pk_columns: Set[str] = set()

        # Match  PRIMARY KEY [CLUSTERED|NONCLUSTERED] ( col-list )
        # Non-greedy .*? ensures we stop at the first ')' (the column-list
        # closing paren), not the WITH(...) paren.
        pk_matches = re.finditer(
            r'PRIMARY\s+KEY\s+(?:CLUSTERED\s+|NONCLUSTERED\s+)?\(\s*(.*?)\s*\)',
            constraint_text,
            re.IGNORECASE | re.DOTALL,
        )

        for m in pk_matches:
            col_list = m.group(1)
            # Each entry may be: [ColName] ASC,  "ColName" DESC,  ColName, etc.
            for col_ref in col_list.split(','):
                col_ref = col_ref.strip()
                # Strip trailing ASC / DESC
                col_ref = re.sub(
                    r'\s+(ASC|DESC)\s*$', '', col_ref, flags=re.IGNORECASE
                ).strip()
                # Strip brackets / quotes
                if len(col_ref) >= 2:
                    if col_ref[0] == '[' and col_ref[-1] == ']':
                        col_ref = col_ref[1:-1]
                    elif col_ref[0] == '"' and col_ref[-1] == '"':
                        col_ref = col_ref[1:-1]
                    elif col_ref[0] == '`' and col_ref[-1] == '`':
                        col_ref = col_ref[1:-1]
                if col_ref:
                    pk_columns.add(col_ref)

        return pk_columns


if __name__ == "__main__":
    # For testing purposes
    logging.basicConfig(level=logging.INFO)
    agent = SchemaAgent(Path("data/schemas"))
    catalogue = agent.run()
    print(f"Found {len(catalogue)} tables")
    for table, info in catalogue.items():
        print(f"  {table}: {info['qualified_name']} ({len(info['columns'])} columns)")