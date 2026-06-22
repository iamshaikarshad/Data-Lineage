"""MasterAgent: decides which extraction method to attempt first for a
given stored procedure, based on objective SQL complexity signals."""

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from .sp_parser import _split_by_top_level_comma

logger = logging.getLogger(__name__)

ExtractionMethod = Literal["regex", "claude", "gemini", "nvidia"]


@dataclass
class ComplexitySignals:
    """Raw signals measured from a single SP's source text."""
    line_count: int = 0
    statement_count: int = 0
    join_count: int = 0
    case_when_count: int = 0
    nested_subquery_count: int = 0
    has_try_catch: bool = False
    has_dynamic_sql: bool = False
    has_cursor: bool = False
    has_pivot_unpivot: bool = False
    has_merge: bool = False
    has_cte: bool = False
    max_select_column_count: int = 0
    bracketed_multiword_identifier_count: int = 0
    score: int = 0
    reasons: list[str] = field(default_factory=list)


class MasterAgent:
    """
    Inspects stored procedure source code and recommends which extraction
    method (regex, claude, gemini, or nvidia) should be attempted FIRST for that
    procedure. Does not perform extraction itself — SPAgent still owns the
    full regex → claude → gemini → nvidia fallback chain; MasterAgent only chooses
    the starting point in that chain.
    """

    # Tunable thresholds
    MAX_LINES_FOR_REGEX = 80
    MAX_STATEMENTS_FOR_REGEX = 1
    MAX_JOINS_FOR_REGEX = 4
    MAX_CASE_WHEN_FOR_REGEX = 6
    MAX_SUBQUERIES_FOR_REGEX = 0
    MAX_SELECT_COLUMNS_FOR_REGEX = 15
    SCORE_THRESHOLD_FOR_LLM = 5

    def __init__(
        self,
        prefer_llm: Literal["claude", "gemini", "nvidia"] = "claude",
    ):
        """
        Args:
            prefer_llm: which LLM to recommend when the router decides an
                LLM is needed. Defaults to "claude".
        """
        self.prefer_llm = prefer_llm

    def analyse(self, sp_code: str) -> ComplexitySignals:
        """
        Measure complexity signals from raw SP source. Operates on the
        ORIGINAL sp_code (not preprocessed) so signals like has_try_catch
        and has_dynamic_sql are detected.
        """
        signals = ComplexitySignals()

        # line_count
        signals.line_count = len(sp_code.splitlines())

        # statement_count: count top-level DML keyword occurrences
        dml_pattern = re.compile(
            r'\b(INSERT\s+INTO|UPDATE|DELETE\s+(?:FROM\s+)?|MERGE(?:\s+INTO)?)\b',
            re.IGNORECASE
        )
        signals.statement_count = len(list(dml_pattern.finditer(sp_code)))

        # join_count
        join_pattern = re.compile(
            r'\b(?:INNER|LEFT|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?JOIN\b',
            re.IGNORECASE
        )
        signals.join_count = len(list(join_pattern.finditer(sp_code)))

        # case_when_count
        case_pattern = re.compile(r'\bCASE\b', re.IGNORECASE)
        signals.case_when_count = len(list(case_pattern.finditer(sp_code)))

        # nested_subquery_count
        subquery_pattern = re.compile(r'\(\s*SELECT\b', re.IGNORECASE)
        signals.nested_subquery_count = len(list(subquery_pattern.finditer(sp_code)))

        # has_try_catch
        signals.has_try_catch = bool(re.search(r'\bBEGIN\s+TRY\b', sp_code, re.IGNORECASE))

        # has_dynamic_sql
        signals.has_dynamic_sql = bool(re.search(
            r'\bsp_executesql\b|\bEXEC\s*\(\s*@', sp_code, re.IGNORECASE
        ))

        # has_cursor
        signals.has_cursor = bool(re.search(
            r'\bDECLARE\s+\w+\s+CURSOR\b', sp_code, re.IGNORECASE
        ))

        # has_pivot_unpivot
        signals.has_pivot_unpivot = bool(re.search(
            r'\b(PIVOT|UNPIVOT)\b', sp_code, re.IGNORECASE
        ))

        # has_merge
        signals.has_merge = bool(re.search(r'\bMERGE\b', sp_code, re.IGNORECASE))

        # has_cte
        signals.has_cte = bool(re.search(
            r'\bWITH\s+\w+\s+AS\s*\(', sp_code, re.IGNORECASE
        ))

        # max_select_column_count
        select_from_pattern = re.compile(
            r'\bSELECT\b(.*?)\bFROM\b', re.IGNORECASE | re.DOTALL
        )
        max_cols = 0
        for m in select_from_pattern.finditer(sp_code):
            col_text = m.group(1)
            cols = _split_by_top_level_comma(col_text)
            max_cols = max(max_cols, len(cols))
        signals.max_select_column_count = max_cols

        # bracketed_multiword_identifier_count
        bracketed_multiword = re.findall(r'\[[^\]]*\s[^\]]*\]', sp_code)
        signals.bracketed_multiword_identifier_count = len(bracketed_multiword)

        # Compute score and reasons
        score = 0
        reasons = []

        if signals.line_count > self.MAX_LINES_FOR_REGEX:
            score += 1
            reasons.append(f"long procedure ({signals.line_count} lines)")

        if signals.statement_count > self.MAX_STATEMENTS_FOR_REGEX:
            score += 1
            reasons.append(f"multiple DML statements ({signals.statement_count})")

        if signals.join_count > self.MAX_JOINS_FOR_REGEX:
            score += 1
            reasons.append(f"many joins ({signals.join_count})")

        if signals.case_when_count > self.MAX_CASE_WHEN_FOR_REGEX:
            score += 1
            reasons.append(f"many CASE expressions ({signals.case_when_count})")

        if signals.nested_subquery_count > self.MAX_SUBQUERIES_FOR_REGEX:
            score += 2
            reasons.append(f"nested subqueries ({signals.nested_subquery_count})")

        if signals.max_select_column_count > self.MAX_SELECT_COLUMNS_FOR_REGEX:
            score += 1
            reasons.append(f"wide SELECT list ({signals.max_select_column_count} columns)")

        if signals.has_dynamic_sql:
            score += 3
            reasons.append("dynamic SQL detected")

        if signals.has_cursor:
            score += 3
            reasons.append("cursor detected")

        if signals.has_pivot_unpivot:
            score += 2
            reasons.append("PIVOT/UNPIVOT detected")

        if signals.has_cte:
            score += 1
            reasons.append("common table expression (WITH) detected")

        if signals.has_merge:
            score += 1
            reasons.append("MERGE statement detected")

        if signals.bracketed_multiword_identifier_count > 10:
            score += 1
            reasons.append(
                f"heavy use of bracketed multi-word identifiers "
                f"({signals.bracketed_multiword_identifier_count})"
            )

        # has_try_catch: +0 points (common, handled by preprocessing)

        signals.score = score
        signals.reasons = reasons

        return signals

    def recommend(self, sp_code: str) -> tuple[ExtractionMethod, ComplexitySignals]:
        """
        Returns (method, signals) where method is "regex", "claude", "gemini",
        or "nvidia" — the recommended STARTING point in the extraction chain.
        """
        signals = self.analyse(sp_code)

        if signals.has_dynamic_sql or signals.has_cursor:
            method = self.prefer_llm
        elif signals.score >= self.SCORE_THRESHOLD_FOR_LLM:
            method = self.prefer_llm
        else:
            method = "regex"

        logger.info(
            f"MasterAgent: score={signals.score} → route={method}  "
            f"reasons={signals.reasons}"
        )

        return method, signals


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO)

    agent = MasterAgent(prefer_llm="claude")

    # Try to load the real Vault.TrustAccessWaits.sql
    vault_path = os.path.join(
        os.path.dirname(__file__), '..', 'data', 'sp', 'Vault.TrustAccessWaits.sql'
    )
    if os.path.exists(vault_path):
        with open(vault_path, 'r', encoding='utf-8', errors='ignore') as f:
            vault_code = f.read()
        method, signals = agent.recommend(vault_code)
        print(f"\nVault.TrustAccessWaits:")
        print(f"  Recommendation: {method}")
        print(f"  Score: {signals.score}")
        print(f"  Reasons: {signals.reasons}")
        print(f"  Signals: line_count={signals.line_count}, "
              f"statements={signals.statement_count}, "
              f"joins={signals.join_count}, "
              f"case_when={signals.case_when_count}, "
              f"subqueries={signals.nested_subquery_count}, "
              f"columns={signals.max_select_column_count}, "
              f"bracketed_ids={signals.bracketed_multiword_identifier_count}")
        print(f"  has_try_catch={signals.has_try_catch}, "
              f"has_dynamic_sql={signals.has_dynamic_sql}, "
              f"has_cursor={signals.has_cursor}")
    else:
        print(f"File not found: {vault_path}")
        # Fallback to inline example
        simple_sp = """
        CREATE PROCEDURE dbo.sp_simple
        AS
        BEGIN
            INSERT INTO [dbo].[Target] (col1, col2)
            SELECT a.col1, a.col2 FROM [dbo].[Source] a;
        END
        """
        method, signals = agent.recommend(simple_sp)
        print(f"\nSimple SP:")
        print(f"  Recommendation: {method}")
        print(f"  Score: {signals.score}")
        print(f"  Reasons: {signals.reasons}")
