"""Utility functions for the data lineage agents."""

import re


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