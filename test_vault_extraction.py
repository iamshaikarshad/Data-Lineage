#!/usr/bin/env python3
"""Test script to extract lineage from Vault.TrustAccessWaits.sql only"""

import os
import sys
import json
from pathlib import Path

# Add current directory to path
sys.path.insert(0, '.')

from agents.schema_agent import SchemaAgent
from agents.sp_agent import SPAgent, load_sp_overrides
from agents.lineage_agent import LineageAgent

def main():
    print("=== Testing Vault.TrustAccessWaits.sql Extraction ===")

    # Set up paths
    base_dir = Path('.')
    sp_dir = base_dir / 'data' / 'sp'
    schema_dir = base_dir / 'data' / 'schemas'

    # Load schema catalogue
    print("Loading schema catalogue...")
    schema_agent = SchemaAgent(schema_dir)
    catalogue = schema_agent.run()
    print(f"Found {len(catalogue)} tables in catalogue")

    # Load overrides (force regex for Vault)
    overrides = {"Vault.TrustAccessWaits.sql": "regex"}
    print(f"Using overrides: {overrides}")

    # Run SPAgent on Vault only
    print("Running SPAgent on Vault.TrustAccessWaits.sql...")
    sp_agent = SPAgent(
        sp_dir=sp_dir,
        catalogue=catalogue,
        anthropic_api_key=None,  # Disable API steps to test regex only
        gemini_api_key=None,
        per_sp_overrides=overrides
    )

    # Read just the Vault file
    vault_path = sp_dir / "Vault.TrustAccessWaits.sql"
    with open(vault_path, 'r', encoding='utf-8', errors='ignore') as f:
        vault_code = f.read()

    # Extract lineage using only regex (since we disabled API keys)
    # We'll call the internal methods directly to bypass the normal flow
    from agents.sp_parser import extract_lineage

    result = extract_lineage(vault_code, catalogue)
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

        # Show some details
        print(f"\nFirst 5 source tables: {result.get('source_tables', [])[:5]}")
        print(f"\nFirst 5 column mappings:")
        for i, mapping in enumerate(result.get('column_mappings', [])[:5]):
            print(f"  {i+1}. {mapping.get('target_column')} <- {mapping.get('source_table')}.{mapping.get('source_column')} [{mapping.get('transformation_type')}]")

        print(f"\nFirst 5 joins:")
        for i, join in enumerate(result.get('joins', [])[:5]):
            print(f"  {i+1}. {join.get('left_table')} {join.get('join_type')} JOIN {join.get('right_table')} ON {join.get('condition')}")

        print(f"\nFirst 5 filters:")
        for i, filter_expr in enumerate(result.get('filters', [])[:5]):
            print(f"  {i+1}. {filter_expr}")

        # Check for specific issues mentioned in the summary
        source_tables = result.get('source_tables', [])
        ssms_in_sources = any('SSMS' in table.upper() for table in source_tables)
        print(f"\nSSMS found in source tables: {ssms_in_sources}")

        # Check if we have the problematic @RecordCountAtSource column mapping
        column_mappings = result.get('column_mappings', [])
        record_count_mappings = [m for m in column_mappings
                               if '@RecordCountAtSource' in str(m.get('source_column', ''))]
        print(f"Mappings with @RecordCountAtSource: {len(record_count_mappings)}")
        if record_count_mappings:
            print(f"  Example: {record_count_mappings[0]}")

        # Check joins for empty left_table
        joins = result.get('joins', [])
        empty_left_joins = [j for j in joins if not j.get('left_table')]
        print(f"Joins with empty left_table: {len(empty_left_joins)}")

        return result
    else:
        print("❌ Extraction failed - no result returned")
        return None

if __name__ == '__main__':
    main()