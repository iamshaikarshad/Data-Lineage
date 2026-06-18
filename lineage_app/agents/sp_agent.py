"""SP Agent that implements the three-step extraction pipeline."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Union

from . import normalise_table
from .sp_parser import extract_lineage

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None
    logger.warning("anthropic package not installed; Claude step will be skipped")

try:
    from google import genai
    _genai_available = True
except ImportError:
    genai = None
    _genai_available = False
    logger.warning("google-genai package not installed; Gemini step will be skipped")


class SPAgent:
    """
    For every .sql file in data/sp/, the agent attempts extraction using three
    steps in order. It moves to the next step only when the previous step fails
    or returns insufficient data.
    """

    def __init__(
        self,
        sp_dir: Union[str, Path],
        catalogue: Dict[str, Dict[str, Any]],
        anthropic_api_key: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        model_claude: str = "claude-sonnet-4-6",
        model_gemini: str = "gemini-1.5-flash",
        delay_between_calls: float = 0.5,
        failure_log_path: Optional[Union[str, Path]] = None,
    ):
        """
        Initialize the SPAgent.

        Args:
            sp_dir: Directory containing stored procedure SQL files.
            catalogue: The table catalogue from SchemaAgent.
            anthropic_api_key: API key for Claude. If None, step 2 is skipped.
            gemini_api_key: API key for Gemini. If None, step 3 is skipped.
            model_claude: Claude model to use.
            model_gemini: Gemini model to use.
            delay_between_calls: Delay between API calls to avoid rate limits.
            failure_log_path: Path to the JSONL failure log. If None, defaults to logs/failed_extractions.jsonl.
        """
        self.sp_dir = Path(sp_dir)
        self.catalogue = catalogue
        self.anthropic_api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.gemini_api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.model_claude = model_claude
        self.model_gemini = model_gemini
        self.delay_between_calls = delay_between_calls
        self.failure_log_path = Path(failure_log_path) if failure_log_path else Path("logs/failed_extractions.jsonl")
        self.results: List[Dict[str, Any]] = []
        self.failure_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize API clients if keys are present
        if self.anthropic_api_key and anthropic:
            self.claude_client = anthropic.Anthropic(api_key=self.anthropic_api_key)
        else:
            self.claude_client = None
            if not self.anthropic_api_key:
                logger.warning("ANTHROPIC_API_KEY not set; skipping Claude extraction step")

        if self.gemini_api_key and _genai_available:
            self.genai_client = genai.Client(api_key=self.gemini_api_key)
            self.gemini_model = self.model_gemini
        else:
            self.genai_client = None
            self.gemini_model = None
            if not self.gemini_api_key:
                logger.warning("GEMINI_API_KEY not set; skipping Gemini extraction step")

    def run(self) -> List[Dict[str, Any]]:
        """
        Iterates over all SP files, applies the three-step pipeline,
        logs failures, returns list of successful results (raw, un-normalised).
        """
        logger.info("Starting SPAgent")
        sp_files = list(self.sp_dir.rglob("*.sql"))
        logger.info(f"Found {len(sp_files)} stored procedure file(s)")

        self.results = []
        failed_count = 0

        for idx, sp_file in enumerate(sp_files, start=1):
            logger.info(f"[{idx}/{len(sp_files)}] Processing {sp_file.name}")
            try:
                sp_code = sp_file.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"Failed to read {sp_file}: {e}")
                continue

            if not sp_code.strip():
                logger.debug(f"Skipping empty file: {sp_file.name}")
                continue

            result = None
            step1_error = None
            step2_error = None
            step3_error = None

            # Step 1: Regex/sqlparse parser
            try:
                result = extract_lineage(sp_code, self.catalogue)
                if result is not None:
                    logger.info(f"  → step1=OK  method=regex")
                    result["extraction_method"] = "regex"
                    result["source_file"] = sp_file.name
                    self.results.append(result)
                    time.sleep(self.delay_between_calls)
                    continue
                else:
                    step1_error = "No INSERT INTO or SELECT INTO found, or missing source tables/column mappings"
            except Exception as e:
                step1_error = f"Unexpected error: {e}"
                logger.error(f"Step 1 raised exception: {e}")

            # Step 2: Claude AI
            if self.claude_client is not None:
                try:
                    result = self._extract_with_claude(sp_code)
                    if result is not None:
                        logger.info(f"  → step1=FAIL  step2=OK  method=claude")
                        result["extraction_method"] = "claude"
                        result["source_file"] = sp_file.name
                        self.results.append(result)
                        time.sleep(self.delay_between_calls)
                        continue
                    else:
                        step2_error = "Returned None (likely JSON decode error)"
                except Exception as e:
                    step2_error = f"API error: {e}"
                    logger.error(f"Step 2 raised exception: {e}")
            else:
                step2_error = "Anthropic API key not set or client not initialized"
                logger.debug("Skipping Claude step: no API key")

            # Step 3: Google Gemini
            if self.gemini_model is not None:
                try:
                    result = self._extract_with_gemini(sp_code)
                    if result is not None:
                        logger.info(f"  → step1=FAIL  step2=FAIL  step3=OK  method=gemini")
                        result["extraction_method"] = "gemini"
                        result["source_file"] = sp_file.name
                        self.results.append(result)
                        time.sleep(self.delay_between_calls)
                        continue
                    else:
                        step3_error = "Returned None (likely JSON decode error)"
                except Exception as e:
                    step3_error = f"API error: {e}"
                    logger.error(f"Step 3 raised exception: {e}")
            else:
                step3_error = "Gemini API key not set or model not initialized"
                logger.debug("Skipping Gemini step: no API key")

            # All three steps failed
            failed_count += 1
            logger.warning(f"  → step1=FAIL  step2=FAIL  step3=FAIL  [REVIEW REQUIRED]")
            self._log_failure(
                sp_file.name,
                sp_code,
                step1_error,
                step2_error,
                step3_error,
            )
            logger.error(f"[REVIEW REQUIRED] {sp_file.name} — all extraction steps failed. See logs/failed_extractions.jsonl for details.")

        logger.info(
            f"SPAgent: {len(self.results)} succeeded "
            f"({sum(1 for r in self.results if r.get('extraction_method') == 'regex')} regex, "
            f"({sum(1 for r in self.results if r.get('extraction_method') == 'claude')} claude, "
            f"({sum(1 for r in self.results if r.get('extraction_method') == 'gemini')} gemini), "
            f"{failed_count} failed"
        )
        return self.results

    def normalised_results(self) -> List[Dict[str, Any]]:
        """
        Returns a copy of self.results with all table names normalised and
        qualified names preserved in parallel fields.
        """
        normalised = []
        for result in self.results:
            # Create a deep copy
            res = json.loads(json.dumps(result))

            # Normalise target_table
            target_table_qualified = res.get("target_table", "")
            res["target_table_qualified"] = target_table_qualified
            res["target_table"] = normalise_table(target_table_qualified)

            # Normalise source_tables
            source_tables = res.get("source_tables", [])
            source_tables_qualified = source_tables[:]  # copy
            res["source_tables_qualified"] = source_tables_qualified
            res["source_tables"] = [normalise_table(t) for t in source_tables]

            # Normalise column_mappings source_table
            for mapping in res.get("column_mappings", []):
                source_table_qualified = mapping.get("source_table", "")
                mapping["source_table_qualified"] = source_table_qualified
                mapping["source_table"] = normalise_table(source_table_qualified)

            # Normalise joins left_table and right_table
            for join in res.get("joins", []):
                left_table_qualified = join.get("left_table", "")
                right_table_qualified = join.get("right_table", "")
                join["left_table_qualified"] = left_table_qualified
                join["right_table_qualized"] = right_table_qualified
                join["left_table"] = normalise_table(left_table_qualified)
                join["right_table"] = normalise_table(right_table_qualified)

            normalised.append(res)
        return normalised

    def _extract_with_claude(self, sp_code: str) -> Optional[Dict[str, Any]]:
        """
        Extract lineage using Claude AI.
        Returns the parsed JSON dict or None on failure.
        """
        if not self.claude_client:
            return None

        # Build the catalogue summary (we'll include a brief version)
        catalogue_summary = ""
        for table_name, info in self.catalogue.items():
            catalogue_summary += f"- {info.get('qualified_name', table_name)}: {len(info.get('columns', []))} columns\n"

        system_prompt = (
            "You are an expert SQL data lineage analyst. "
            "Your job is to read a stored procedure and return ONLY valid JSON describing "
            "the data lineage — no markdown, no explanation, just the JSON object."
        )

        user_prompt = f"""
	Analyse this stored procedure. Return ONLY a JSON object
	(no markdown fences, no extra text) with EXACTLY this structure:

	{{
	  "procedure_name": "<name of the SP>",
	  "target_table": "<the table written to — use the full qualified name as it appears>",
	  "source_tables": ["<full qualified name as it appears>", ...],
	  "column_mappings": [
	    {{
	      "target_column": "<column in target table>",
	      "source_table":  "<full qualified name as it appears>",
	      "source_column": "<original column name or full expression>",
	      "transformation_type": "direct_copy | aggregation | calculation | conditional | constant",
	      "transformation_logic": "<brief plain-English description>"
	    }}
	  ],
	  "joins": [
	    {{
	      "left_table":  "<full qualified name as it appears>",
	      "right_table": "<full qualified name as it appears>",
	      "join_type":   "INNER | LEFT | RIGHT | FULL | CROSS",
	      "condition":   "<the ON clause verbatim>"
	    }}
	  ],
	  "filters":  ["<each WHERE condition as a string>"],
	  "grouping": ["<each GROUP BY expression as a string>"]
	}}

	STORED PROCEDURE:
	```sql
	{sp_code}
	```

	KNOWN TABLE CATALOGUE (for reference only — use qualified names from SP, not these):
	{catalogue_summary}
	""".strip()

        try:
            response = self.claude_client.messages.create(
                model=self.model_claude,
                max_tokens=4096,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": user_prompt,
                    }
                ],
            )
            # Extract the text content
            text = response.content[0].text if response.content else ""
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None

        # Strip markdown fences
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
            # Validate that we have the required keys? We'll assume the LLM follows the structure.
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            logger.debug(f"Claude response text: {text}")
            return None

    def _extract_with_gemini(self, sp_code: str) -> Optional[Dict[str, Any]]:
        """
        Extract lineage using Google Gemini.
        Returns the parsed JSON dict or None on failure.
        """
        if not self.genai_client:
            return None

        # Build the catalogue summary
        catalogue_summary = ""
        for table_name, info in self.catalogue.items():
            catalogue_summary += f"- {info.get('qualified_name', table_name)}: {len(info.get('columns', []))} columns\n"

        # The prompt is the same as for Claude, but we prefix with SYSTEM and USER as per instructions
        system_text = "You are an expert SQL data lineage analyst. Return ONLY valid JSON, no markdown."
        user_text = f"""
	Analyse this stored procedure. Return ONLY a JSON object
	(no markdown fences, no extra text) with EXACTLY this structure:

	{{
	  "procedure_name": "<name of the SP>",
	  "target_table": "<the table written to — use the full qualified name as it appears>",
	  "source_tables": ["<full qualified name as it appears>", ...],
	  "column_mappings": [
	    {{
	      "target_column": "<column in target table>",
	      "source_table":  "<full qualified name as it appears>",
	      "source_column": "<original column name or full expression>",
	      "transformation_type": "direct_copy | aggregation | calculation | conditional | constant",
	      "transformation_logic": "<brief plain-English description>"
	    }}
	  ],
	  "joins": [
	    {{
	      "left_table":  "<full qualified name as it appears>",
	      "right_table": "<full qualified name as it appears>",
	      "join_type":   "INNER | LEFT | RIGHT | FULL | CROSS",
	      "condition":   "<the ON clause verbatim>"
	    }}
	  ],
	  "filters":  ["<each WHERE condition as a string>"],
	  "grouping": ["<each GROUP BY expression as a string>"]
	}}

	STORED PROCEDURE:
	```sql
	{sp_code}
	```

	KNOWN TABLE CATALOGUE (for reference only — use qualified names from SP, not these):
	{catalogue_summary}
	""".strip()

        prompt = f"SYSTEM: {system_text}\n\nUSER: {user_text}"

        try:
            response = self.genai_client.models.generate_content(
                model=self.gemini_model,
                contents=prompt
            )
            text = response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return None

        # Strip markdown fences
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            logger.debug(f"Gemini response text: {text}")
            return None

    def _log_failure(
        self,
        source_file: str,
        sp_code: str,
        step1_error: Optional[str],
        step2_error: Optional[str],
        step3_error: Optional[str],
    ) -> None:
        """
        Append a structured failure record to the JSONL log.
        """
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_file": source_file,
            "failure_reason": "All three extraction steps failed",
            "step1_error": step1_error or "Unknown",
            "step2_error": step2_error or "Skipped or unknown",
            "step3_error": step3_error or "Skipped or unknown",
            "sp_snippet": sp_code[:300],
        }
        try:
            with self.failure_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Failed to write to failure log: {e}")


if __name__ == "__main__":
    # For testing
    logging.basicConfig(level=logging.INFO)
    # We need a catalogue; we'll create a mock one
    catalogue = {
        "CUSTOMERS": {
            "qualified_name": "[SalesDB].[dbo].[Customers]",
            "database": "SALESDB",
            "schema": "DBO",
            "columns": [{"name": "CUSTOMER_ID", "type": "INT", "nullable": False, "primary_key": True}],
            "source_file": "tables.sql"
        }
    }
    # We don't have API keys in this test environment, so we'll only run step 1
    agent = SPAgent(
        sp_dir="data/sp",
        catalogue=catalogue,
        anthropic_api_key=None,
        gemini_api_key=None,
    )
    results = agent.run()
    print(f"Results: {results}")