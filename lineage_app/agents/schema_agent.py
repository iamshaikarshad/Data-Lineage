"""SchemaAgent for parsing CREATE TABLE statements and building a table catalogue."""

import logging
import re
from pathlib import Path
from typing import Dict, List, Any, Union

from . import normalise_table

logger = logging.getLogger(__name__)


class SchemaAgent:
    """
    Reads every .sql file under data/schemas/ (recursively), parses every
    CREATE TABLE statement it finds, and returns a catalogue dictionary.
    """

    def __init__(self, schema_dir: Union[str, Path]):
        """
        Initialize the SchemaAgent.

        Args:
            schema_dir: Path to the directory containing schema SQL files.
        """
        self.schema_dir = Path(schema_dir)
        self.catalogue: Dict[str, Dict[str, Any]] = {}

    def run(self) -> Dict[str, Dict[str, Any]]:
        """
        Parse all schema files and return the catalogue.

        Returns:
            A dictionary mapping normalised table names to their metadata.
        """
        logger.info("Starting SchemaAgent")
        schema_files = list(self.schema_dir.rglob("*.sql"))
        logger.info(f"Found {len(schema_files)} schema file(s)")

        for schema_file in schema_files:
            self._parse_file(schema_file)

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
        # The first element might be empty or text before the first CREATE TABLE
        for stmt in statements:
            if not stmt.strip():
                continue
            if not re.match(r'CREATE\s+TABLE\b', stmt, flags=re.IGNORECASE):
                # Skip any non-CREATE TABLE chunks
                continue
            self._parse_create_table(stmt, file_path.name)

    def _parse_create_table(self, stmt: str, source_file: str) -> None:
        """
        Parse a single CREATE TABLE statement.

        Args:
            stmt: The CREATE TABLE statement string.
            source_file: The name of the file the statement came from.
        """
        # Regex to capture database, schema, and table name
        # Handles optional database and schema parts, each optionally surrounded by [] or ""
        pattern = re.compile(
            r'''CREATE\s+TABLE\s+
            (?:\[?\"?(\w+)\"?\]?\.)?      # optional database (group 1)
            (?:\[?\"?(\w+)\"?\]?\.)?      # optional schema (group 2)
            \[?\"?(\w+)\"?\]?             # table name (group 3)
            ''',
            re.IGNORECASE | re.VERBOSE
        )
        match = pattern.search(stmt)
        if not match:
            logger.warning(f"Could not parse table name from statement: {stmt[:100]}...")
            return

        db_part, schema_part, table_part = match.groups()
        table_name = normalise_table(table_part) if table_part else ""
        if not table_name:
            logger.warning(f"Empty table name after normalisation in: {stmt[:100]}...")
            return

        # Build qualified name as it appears (original)
        qualified_parts = [p for p in [db_part, schema_part, table_part] if p is not None]
        qualified_name = ".".join(qualified_parts)

        # Normalise database and schema parts (if present) to uppercase, else empty string
        database = normalise_table(db_part) if db_part else ""
        schema = normalise_table(schema_part) if schema_part else ""

        # Find the column body by matching parentheses
        # We look for the first '(' after CREATE TABLE and then find the matching ')'
        # We assume the statement ends with a semicolon or the end of string.
        # We'll find the start of the column list.
        # We look for the first '(' that is after the table name.
        # Since we have the match, we know the table name ends at match.end(3)
        start_idx = stmt.find('(', match.end(3))
        if start_idx == -1:
            logger.warning(f"No opening parenthesis for columns in: {stmt[:100]}...")
            return

        # Now find the matching closing parenthesis
        depth = 0
        in_string = False
        string_char = None
        escape = False
        for i, ch in enumerate(stmt[start_idx:], start=start_idx):
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch in ('\"', "'"):
                if not in_string:
                    in_string = True
                    string_char = ch
                elif string_char == ch:
                    in_string = False
                    string_char = None
                continue
            if in_string:
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        else:
            logger.warning(f"Could not find matching closing parenthesis for columns in: {stmt[:100]}...")
            return

        column_body = stmt[start_idx + 1:end_idx]

        # Now parse each line in the column body
        lines = column_body.split('\n')
        columns: List[Dict[str, Any]] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip constraint lines
            upper_line = line.upper()
            if any(upper_line.startswith(keyword) for keyword in
                   ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY', 'INDEX', 'CONSTRAINT', 'CHECK')):
                continue
            # Skip comments
            if upper_line.startswith('--'):
                continue

            # Parse column definition: [name] [type] [nullable?] [primary key?]
            # We'll use a regex to capture the column name and data type
            col_pattern = re.compile(
                r'''\[?\"?(\w+)\"?\]?\s+   # column name
                (\w+(?:\s*\(\s*[\d,\s]+\s*\))?)  # data type with optional parameters
                ''',
                re.VERBOSE
            )
            col_match = col_pattern.match(line)
            if not col_match:
                # If we can't parse, skip the line
                logger.debug(f"Skipping unrecognised column line: {line}")
                continue

            col_name, data_type = col_match.groups()
            col_name = normalise_table(col_name) if col_name else ""
            data_type = data_type.upper().strip()

            # Determine nullable and primary key
            nullable = True
            primary_key = False

            # Check for NOT NULL
            if re.search(r'\bNOT\s+NULL\b', line, re.IGNORECASE):
                nullable = False
            # Check for inline PRIMARY KEY
            if re.search(r'\bPRIMARY\s+KEY\b', line, re.IGNORECASE):
                primary_key = True
                nullable = False  # Primary key implies NOT NULL

            columns.append({
                "name": col_name,
                "type": data_type,
                "nullable": nullable,
                "primary_key": primary_key
            })

        # Store in catalogue
        self.catalogue[table_name] = {
            "qualified_name": qualified_name,
            "database": database,
            "schema": schema,
            "columns": columns,
            "source_file": source_file
        }

        logger.debug(f"Parsed table: {table_name} from {source_file}")


if __name__ == "__main__":
    # For testing purposes
    logging.basicConfig(level=logging.INFO)
    agent = SchemaAgent(Path("data/schemas"))
    catalogue = agent.run()
    print(f"Found {len(catalogue)} tables")
    for table, info in catalogue.items():
        print(f"  {table}: {info['qualified_name']} ({len(info['columns'])} columns)")