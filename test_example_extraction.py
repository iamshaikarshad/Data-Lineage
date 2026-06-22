#!/usr/bin/env python3
"""Test script to extract lineage from sp_example.sql only"""

import os
import sys
import json
from pathlib import Path

# Add current directory to path
sys.path.insert(0, '.')

from agents.sp_parser import extract_lineage

def main():
    print("=== Testing sp_example.sql Extraction ===")

    # Load a basic catalogue for testing
    catalogue = {
        "CUSTOMERS": {
            "qualified_name": "[SalesDB].[dbo].[Customers]",
            "database": "SALESDB", "schema": "DBO",
            "columns": [{"name": "CUSTOMER_ID", "type": "INT", "nullable": False, "primary_key": True},
                       {"name": "CUSTOMER_NAME", "type": "VARCHAR(100)", "nullable": False},
                       {"name": "GENDER_ID", "type": "INT", "nullable": False}],
            "source_file": "tables.sql"
        },
        "ORDERS": {
            "qualified_name": "[SalesDB].[dbo].[Orders]",
            "database": "SALESDB", "schema": "DBO",
            "columns": [{"name": "ORDER_ID", "type": "INT", "nullable": False, "primary_key": True},
                       {"name": "CUSTOMER_ID", "type": "INT", "nullable": False},
                       {"name": "ORDER_AMOUNT", "type": "DECIMAL(10,2)", "nullable": False},
                       {"name": "ORDER_DATE", "type": "DATE", "nullable": False}],
            "source_file": "tables.sql"
        }
    }

    # Read the example file
    example_path = Path("../data/sp/sp_example.sql")
    with open(example_path, 'r', encoding='utf-8', errors='ignore') as f:
        example_code = f.read()

    print("Input SQL:")
    print(example_code[:200] + "..." if len(example_code) > 200 else example_code)
    print()

    # Extract lineage
    result = extract_lineage(example_code, catalogue)
    if result:
        print("\n=== EXTRACTION RESULT ===")
        print(f"Procedure name: {result.get('procedure_name')}")
        print(f"Target table: {result.get('target_table')}")
        print(f"Source tables: {result.get('source_tables')}")
        print(f"Number of source tables: {len(result.get('source_tables', []))}")
        print(f"Number of column mappings: {len(result.get('column_mappings', []))}")
        print(f"Number of joins: {len(result.get('joins', []))}")
        print(f"Number of filters: {len(result.get('filters', []))}")
        print(f"Number of grouping: {len(result.get('grouping', []))}")

        # Show details
        print(f"\nSource tables: {result.get('source_tables', [])}")
        print(f"\nColumn mappings:")
        for i, mapping in enumerate(result.get('column_mappings', [])):
            print(f"  {i+1}. {mapping.get('target_column')} <- {mapping.get('source_table')}.{mapping.get('source_column')} [{mapping.get('transformation_type')}]")

        print(f"\nJoins:")
        for i, join in enumerate(result.get('joins', [])):
            print(f"  {i+1}. {join.get('left_table')} {join.get('join_type')} JOIN {join.get('right_table')} ON {join.get('condition')}")

        print(f"\nFilters:")
        for i, filter_expr in enumerate(result.get('filters', [])):
            print(f"  {i+1}. {filter_expr}")

        print(f"\nGrouping:")
        for i, group_expr in enumerate(result.get('grouping', [])):
            print(f"  {i+1}. {group_expr}")

        return result
    else:
        print("❌ Extraction failed - no result returned")
        return None

if __name__ == '__main__':
    main()