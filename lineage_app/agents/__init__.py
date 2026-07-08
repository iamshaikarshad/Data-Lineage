"""Utility functions for the data lineage agents."""

import re


def strip_brackets(identifier: str) -> str:
    """
    Remove every SQL Server square bracket from an identifier, preserving
    everything else (including any dotted schema.table qualification).

    Use this at the point where identifiers are captured from SP text so
    downstream consumers never see bracketed names. Unlike
    ``normalise_table`` it does NOT collapse a qualified name to its last
    segment nor change case -- it only removes the brackets.

    Examples:
        "[TableName]"        -> "TableName"
        "[dbo].[TableName]"  -> "dbo.TableName"
        "[Vault].Patient"    -> "Vault.Patient"
        "[ColumnName]"       -> "ColumnName"
        "TableName"          -> "TableName"
        "[a].[RTT Start Date]" -> "a.RTT Start Date"

    Non-string input is returned unchanged.
    """
    if not isinstance(identifier, str):
        return identifier
    return identifier.replace('[', '').replace(']', '')


def normalise_table(name: str) -> str:
    """
    Normalise a table reference to uppercase table name only.

    Examples:
        Customers -> CUSTOMERS
        dbo.Customers -> CUSTOMERS
        [dbo].[Customers] -> CUSTOMERS
        [MyDB].[dbo].[Customers] -> CUSTOMERS
        "MyDB"."dbo"."Customers" -> CUSTOMERS
    """
    if not isinstance(name, str):
        return name

    # 1. Strip leading/trailing whitespace
    name = name.strip()

    # 2. Replace every " with nothing
    name = name.replace('"', '')

    # 3. Split on . and take the last segment
    parts = name.split('.')
    if parts:
        name = parts[-1]
    else:
        name = ''

    # 4. Strip surrounding [ and ]
    name = name.strip('[]')

    # 5. Strip remaining whitespace
    name = name.strip()

    # 6. Return uppercased
    return name.upper()