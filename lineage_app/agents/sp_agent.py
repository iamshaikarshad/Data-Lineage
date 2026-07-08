"""SP Agent that implements the three-step extraction pipeline with
MasterAgent routing and per-SP override support."""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Union, Literal

from . import normalise_table
from .sp_parser import extract_lineage
from .master_agent import MasterAgent, ExtractionMethod

logger = logging.getLogger(__name__)


def _strip_llm_response_to_json(text: str) -> Optional[str]:
    """Extract the first valid JSON object from an LLM response.

    LLMs often wrap JSON in markdown fences or add prose around it.
    This helper strips fences, finds the outermost ``{ … }`` block,
    and returns it; returns None if no JSON object can be found.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()

    # Strip common markdown fence patterns
    #   ```json\n{...}\n```   or   ```\n{...}\n```
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL
    )
    if fence_match:
        raw = fence_match.group(1).strip()

    # If it still doesn't start with '{', try to find the first '{'
    # and match its closing '}' (handling nesting).
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start : i + 1]
                # Validate it parses
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    # Keep searching for a later block
                    continue
    # No balanced JSON object found
    return raw[start:] if start != -1 else None

try:
    import anthropic
except ImportError:
    anthropic = None
    logger.warning("anthropic package not installed; Claude step will be skipped")

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    logger.warning("openai package not installed; Gemini/NVIDIA step will be skipped")

try:
    from google import genai
    _genai_available = True
except ImportError:
    genai = None
    _genai_available = False
    logger.warning("google-genai package not installed; Gemini step will be skipped")


def load_sp_overrides(path: Union[str, Path]) -> Dict[str, str]:
    """
    Load the per-SP extraction-method override file. Returns an empty dict
    if the file does not exist or is invalid JSON (logs a WARNING in the
    invalid-JSON case, stays silent if simply missing).
    Validates that every value is one of "regex", "claude", "gemini","nvidia";
    invalid values are dropped with a WARNING.
    """
    path = Path(path)
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in override file %s: %s", path, e)
        return {}
    except Exception as e:
        logger.warning("Failed to read override file %s: %s", path, e)
        return {}

    if not isinstance(data, dict):
        logger.warning("Override file %s must contain a JSON object, got %s", path, type(data).__name__)
        return {}

    valid_methods = {"regex", "claude", "gemini","nvidia"}
    overrides = {}
    for key, value in data.items():
        if isinstance(value, str) and value.lower() in valid_methods:
            overrides[key] = value.lower()
        else:
            logger.warning(
                "Override file: dropping invalid entry '%s': '%s' "
                "(value must be one of %s)", key, value, valid_methods
            )

    return overrides


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
        nvidia_api_key: Optional[str] = None,
        model_claude: str = "claude-sonnet-4-6",
        model_gemini: str = "gemini-2.5-flash",
        model_nvidia: str = "nvidia/nemotron-3-super-120b-a12b",
        delay_between_calls: float = 0.5,
        failure_log_path: Optional[Union[str, Path]] = None,
        use_master_agent: bool = True,
        prefer_llm: Literal["claude", "gemini", "nvidia"] = "gemini",
        per_sp_overrides: Optional[Dict[str, ExtractionMethod]] = None,
        tracker: Any = None,
    ):
        """
        Initialize the SPAgent.

        Args:
            sp_dir: Directory containing stored procedure SQL files.
            catalogue: The table catalogue from SchemaAgent.
            anthropic_api_key: API key for Claude. If None, step 2 is skipped.
            gemini_api_key: API key for Gemini. If None, step 3 is skipped.
            nvidia_api_key: API key for NVIDIA NIM. If None, step 4 is skipped.
            model_claude: Claude model to use.
            model_gemini: Gemini model to use.
            model_nvidia: NVIDIA NIM model to use.
            delay_between_calls: Delay between API calls to avoid rate limits.
            failure_log_path: Path to the JSONL failure log.
            use_master_agent: If True, use MasterAgent to decide starting method.
            prefer_llm: Which LLM to prefer when MasterAgent routes to LLM.
            per_sp_overrides: Dict mapping filename -> forced extraction method.
            tracker: Optional ProcessingTracker to skip already-processed files.
        """
        self.sp_dir = Path(sp_dir)
        self.catalogue = catalogue
        self.anthropic_api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.gemini_api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.nvidia_api_key = nvidia_api_key or os.environ.get("NVIDIA_API_KEY")
        self.model_claude = model_claude
        self.model_gemini = model_gemini
        self.model_nvidia = model_nvidia
        self.delay_between_calls = delay_between_calls
        self.failure_log_path = Path(failure_log_path) if failure_log_path else Path("logs/failed_extractions.jsonl")
        self.results: List[Dict[str, Any]] = []
        self.failure_log_path.parent.mkdir(parents=True, exist_ok=True)

        self.use_master_agent = use_master_agent
        self.master_agent = MasterAgent(prefer_llm=prefer_llm) if use_master_agent else None
        self.per_sp_overrides = per_sp_overrides or {}
        self.tracker = tracker

        # Track which methods are actually available
        available: Dict[str, bool] = {"regex": True}

        # Initialize API clients if keys are present
        if self.anthropic_api_key and anthropic:
            self.claude_client = anthropic.Anthropic(api_key=self.anthropic_api_key)
            available["claude"] = True
        else:
            self.claude_client = None
            available["claude"] = False
            if not self.anthropic_api_key:
                logger.warning("ANTHROPIC_API_KEY not set; skipping Claude extraction step")

        if self.gemini_api_key and _genai_available:
            self.genai_client = genai.Client(api_key=self.gemini_api_key)
            self.gemini_model = self.model_gemini
            available["gemini"] = True
        else:
            self.genai_client = None
            self.gemini_model = None
            available["gemini"] = False
            if not self.gemini_api_key:
                logger.warning("GEMINI_API_KEY not set; skipping Gemini extraction step")

        if self.nvidia_api_key and OpenAI is not None:
            self.nvidia_client = OpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=self.nvidia_api_key,
            )
            available["nvidia"] = True
        else:
            self.nvidia_client = None
            available["nvidia"] = False
            if not self.nvidia_api_key:
                logger.warning("NVIDIA_API_KEY not set; skipping NVIDIA extraction step")

        self._available_methods = available

    def _build_step_sequence(self, start_method: str, is_override: bool = False) -> List[tuple]:
        """
        Returns the ordered list of (method_name, extractor_callable) pairs
        to attempt.

        When is_override is False (MasterAgent or default routing):
          Start at start_method in the canonical regex -> claude -> gemini
          order and include every step that follows it.

        When is_override is True (user forced a specific method):
          Put the override method first, then add all remaining available
          methods as fallback in canonical order.  This way, forcing
          "gemini" tries gemini first but falls back to claude/regex if
          it fails, rather than producing no result at all.

        If the requested start_method is entirely unavailable (client/key
        missing), an override inserts a stub that raises a clear error so
        the failure is explicitly logged; a non-override silently falls
        back to the first available step.
        """
        all_steps = []
        all_steps.append(("regex", lambda code: extract_lineage(code, self.catalogue)))
        if self.claude_client is not None:
            all_steps.append(("claude", self._extract_with_claude))
        if self.gemini_model is not None:
            all_steps.append(("gemini", self._extract_with_gemini))
        if self.nvidia_client is not None:
            all_steps.append(("nvidia", self._extract_with_nvidia))

        method_order = [name for name, _ in all_steps]

        if is_override:
            # Build sequence: override method first, then everything else
            # in canonical order as fallback.
            sequence: List[tuple] = []

            if start_method in method_order:
                # Override method is available — put it first
                idx = method_order.index(start_method)
                sequence.append(all_steps[idx])
                # Add all other available steps in canonical order
                for i, step in enumerate(all_steps):
                    if i != idx:
                        sequence.append(step)
            else:
                # Override method unavailable (no client/key).
                # Insert a stub that raises so the failure is explicit,
                # then add all available steps as fallback.
                logger.warning(
                    "  -> user override requested '%s' but client/key is "
                    "unavailable; will try to fall back to other methods",
                    start_method,
                )
                def _unavailable_stub(code, _method=start_method):
                    raise RuntimeError(
                        "Extraction method '%s' was forced by override "
                        "but the API client/key is not configured" % _method
                    )
                sequence.append((start_method, _unavailable_stub))
                # Add all available steps as fallback
                sequence.extend(all_steps)

            return sequence

        # Non-override path: start at the given method in canonical order
        if start_method in method_order:
            start_idx = method_order.index(start_method)
        else:
            logger.warning(
                "  -> requested start method '%s' unavailable "
                "(missing client/key); falling back to first available step",
                start_method,
            )
            start_idx = 0
        return all_steps[start_idx:]

    def run(self) -> List[Dict[str, Any]]:
        """
        Iterates over all SP files, applies the extraction pipeline,
        logs failures, returns list of successful results (raw, un-normalised).

        If a ProcessingTracker is attached, already-processed files are
        skipped entirely.
        """
        logger.info("Starting SPAgent")
        all_sp_files = list(self.sp_dir.rglob("*.sql"))
        logger.info("Found %d stored procedure file(s)", len(all_sp_files))

        # Filter out already-processed files when tracker is available
        if self.tracker:
            unprocessed = [f for f in all_sp_files if not self.tracker.is_sp_processed(f.name)]
            skipped = len(all_sp_files) - len(unprocessed)
            if skipped:
                logger.info(
                    "Tracker: skipping %d already-processed SP(s), processing %d new",
                    skipped, len(unprocessed),
                )
                for f in all_sp_files:
                    if self.tracker.is_sp_processed(f.name):
                        logger.info("  Skipping already-processed: %s", f.name)
            sp_files = unprocessed
        else:
            sp_files = all_sp_files

        self.results = []
        failed_count = 0
        routing_counts: Dict[str, int] = {"regex": 0, "claude": 0, "gemini": 0, "nvidia": 0}

        for idx, sp_file in enumerate(sp_files, start=1):
            logger.info("[%d/%d] Processing %s", idx, len(sp_files), sp_file.name)
            try:
                sp_code = sp_file.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                logger.error("Failed to read %s: %s", sp_file, e)
                continue

            if not sp_code.strip():
                logger.debug("Skipping empty file: %s", sp_file.name)
                continue

            # Determine starting extraction method for this file
            is_override = False
            override = self.per_sp_overrides.get(sp_file.name)
            if override:
                start_method = override
                is_override = True
                logger.info("  -> user override: forcing method=%s for %s", override, sp_file.name)
            elif self.master_agent is not None:
                start_method, signals = self.master_agent.recommend(sp_code)
                logger.info(
                    "  -> MasterAgent: score=%s -> route=%s  reasons=%s",
                    signals.score, start_method, signals.reasons,
                )
            else:
                start_method = "regex"

            routing_counts[start_method] = routing_counts.get(start_method, 0) + 1

            # Execute extraction steps
            step_errors: Dict[str, Optional[str]] = {
                "regex": None, "claude": None, "gemini": None, "nvidia": None
            }
            steps_attempted: List[str] = []
            result = None

            for method_name, extractor in self._build_step_sequence(start_method, is_override=is_override):
                steps_attempted.append(method_name)
                try:
                    result = extractor(sp_code)
                    if result is not None:
                        logger.info("  -> method=%s  steps_tried=%s", method_name, steps_attempted)
                        result["extraction_method"] = method_name
                        result["source_file"] = sp_file.name
                        self.results.append(result)
                        break
                    else:
                        step_errors[method_name] = "Returned None"
                except Exception as e:
                    step_errors[method_name] = "Exception: %s" % e
                    logger.error("  -> %s raised exception: %s", method_name, e)

            if result is None:
                for skipped in {"regex", "claude", "gemini", "nvidia"} - set(steps_attempted):
                    step_errors[skipped] = "Skipped (not attempted, started at later step)"
                failed_count += 1
                logger.warning(
                    "  -> all attempted steps failed  steps_tried=%s  [REVIEW REQUIRED]",
                    steps_attempted,
                )
                self._log_failure(
                    sp_file.name, sp_code,
                    step_errors["regex"], step_errors["claude"],
                    step_errors["gemini"], step_errors["nvidia"],
                )
                # Mark failure in tracker
                if self.tracker:
                    self.tracker.mark_sp(sp_file.name, "failed")
            else:
                # Mark success in tracker
                if self.tracker:
                    self.tracker.mark_sp(
                        sp_file.name, "success",
                        method=result.get("extraction_method", ""),
                        target_table=result.get("target_table", ""),
                    )
                time.sleep(self.delay_between_calls)

        # Persist tracker state
        if self.tracker:
            self.tracker.save()

        logger.info(
            "SPAgent: %d succeeded "
            "(%d regex, %d claude, %d gemini, %d nvidia), "
            "%d failed, routing_counts=%s",
            len(self.results),
            sum(1 for r in self.results if r.get("extraction_method") == "regex"),
            sum(1 for r in self.results if r.get("extraction_method") == "claude"),
            sum(1 for r in self.results if r.get("extraction_method") == "gemini"),
            sum(1 for r in self.results if r.get("extraction_method") == "nvidia"),
            failed_count,
            routing_counts,
        )
        return self.results

    def normalised_results(self) -> List[Dict[str, Any]]:
        """
        Returns a copy of self.results with all table names normalised and
        qualified names preserved in parallel fields.
        """
        normalised = []
        for result in self.results:
            res = json.loads(json.dumps(result))

            target_table_qualified = res.get("target_table", "")
            res["target_table_qualified"] = target_table_qualified
            res["target_table"] = normalise_table(target_table_qualified)

            source_tables = res.get("source_tables", [])
            source_tables_qualified = source_tables[:]
            res["source_tables_qualified"] = source_tables_qualified
            res["source_tables"] = [normalise_table(t) for t in source_tables]

            for mapping in res.get("column_mappings", []):
                source_table_qualified = mapping.get("source_table", "")
                mapping["source_table_qualified"] = source_table_qualified
                mapping["source_table"] = normalise_table(source_table_qualified)

            for join in res.get("joins", []):
                left_table_qualified = join.get("left_table", "")
                right_table_qualified = join.get("right_table", "")
                join["left_table_qualified"] = left_table_qualified
                join["right_table_qualified"] = right_table_qualified
                join["left_table"] = normalise_table(left_table_qualified)
                join["right_table"] = normalise_table(right_table_qualified)

            normalised.append(res)
        return normalised

    def _extract_with_claude(self, sp_code: str) -> Optional[Dict[str, Any]]:
        """Extract lineage using Claude AI. Returns the parsed JSON dict or None."""
        if not self.claude_client:
            return None

        catalogue_summary = ""
        for table_name, info in self.catalogue.items():
            catalogue_summary += "- %s: %d columns\n" % (
                info.get("qualified_name", table_name),
                len(info.get("columns", [])),
            )

        system_prompt = (
            "You are an expert SQL data lineage analyst. "
            "Your job is to read a stored procedure and return ONLY valid JSON describing "
            "the data lineage -- no markdown, no explanation, just the JSON object."
        )

        user_prompt = (
            "Analyse this stored procedure. Return ONLY a JSON object "
            "(no markdown fences, no extra text) with EXACTLY this structure:\n\n"
            "{\n"
            '  "procedure_name": "<name of the SP>",\n'
            '  "target_table": "<the table written to -- use the full qualified name as it appears>",\n'
            '  "source_tables": ["<full qualified name as it appears>", ...],\n'
            '  "column_mappings": [\n'
            "    {\n"
            '      "target_column": "<column in target table>",\n'
            '      "source_table":  "<full qualified name as it appears>",\n'
            '      "source_column": "<actual column name NOT the SQL alias -- e.g. Gender_ID not a.Gender_ID or p.Gender_ID>",\n'
            '      "transformation_type": "direct_copy | aggregation | calculation | conditional | constant",\n'
            '      "transformation_logic": "<brief plain-English description>"\n'
            "    }\n"
            "  ],\n"
            '  "joins": [\n'
            "    {\n"
            '      "left_table":  "<full qualified name as it appears>",\n'
            '      "right_table": "<full qualified name as it appears>",\n'
            '      "join_type":   "INNER | LEFT | RIGHT | FULL | CROSS",\n'
            '      "condition":   "<the ON clause using real table names, NOT aliases -- e.g. Patient.PatientID = Referral.PatientID not a.PatientID = b.PatientID>"\n'
            "    }\n"
            "  ],\n"
            '  "filters":  ["<each WHERE condition as a string>"],\n'
            '  "grouping": ["<each GROUP BY expression as a string>"]\n'
            "}\n\n"
            "CRITICAL RULE — NO ALIASES:\n"
            "SQL aliases (like 'a', 'b', 'p', 't1') are shorthand used inside the SP, but they are "
            "meaningless outside its context. You MUST resolve every alias to the actual table name it "
            "refers to:\n"
            "  - In source_column: use TableName.ColumnName, never alias.ColumnName\n"
            "  - In condition: use real table names, never alias-prefixed columns\n"
            "  - Example: if the SP says 'SELECT a.Gender_ID FROM [Vault].Patient a', the "
            "source_column must be 'Gender_ID', NOT 'a.Gender_ID'\n\n"
            "STORED PROCEDURE:\n"
            "```sql\n%s\n```\n\n"
            "KNOWN TABLE CATALOGUE (for reference only -- use qualified names from SP, not these):\n%s"
        ) % (sp_code, catalogue_summary)

        try:
            response = self.claude_client.messages.create(
                model=self.model_claude,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = response.content[0].text if response.content else ""
        except Exception as e:
            logger.error("Claude API error: %s", e)
            return None

        cleaned = _strip_llm_response_to_json(text)
        if cleaned is None:
            logger.error("Failed to extract JSON from Claude response (empty or no JSON object found)")
            logger.debug("Claude raw response: %s", text[:500])
            return None

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Claude response as JSON: %s", e)
            logger.debug("Claude response text: %s", text[:500])
            return None

    def _extract_with_gemini(self, sp_code: str) -> Optional[Dict[str, Any]]:
        """Extract lineage using Google Gemini. Returns the parsed JSON dict or None."""
        if not self.genai_client:
            return None

        catalogue_summary = ""
        for table_name, info in self.catalogue.items():
            catalogue_summary += "- %s: %d columns\n" % (
                info.get("qualified_name", table_name),
                len(info.get("columns", [])),
            )

        system_text = "You are an expert SQL data lineage analyst. Return ONLY valid JSON, no markdown."
        user_text = (
            "Analyse this stored procedure. Return ONLY a JSON object "
            "(no markdown fences, no extra text) with EXACTLY this structure:\n\n"
            "{\n"
            '  "procedure_name": "<name of the SP>",\n'
            '  "target_table": "<the table written to -- use the full qualified name as it appears>",\n'
            '  "source_tables": ["<full qualified name as it appears>", ...],\n'
            '  "column_mappings": [\n'
            "    {\n"
            '      "target_column": "<column in target table>",\n'
            '      "source_table":  "<full qualified name as it appears>",\n'
            '      "source_column": "<actual column name, NOT the SQL alias -- e.g. Gender_ID not a.Gender_ID or p.Gender_ID>",\n'
            '      "transformation_type": "direct_copy | aggregation | calculation | conditional | constant",\n'
            '      "transformation_logic": "<brief plain-English description>"\n'
            "    }\n"
            "  ],\n"
            '  "joins": [\n'
            "    {\n"
            '      "left_table":  "<full qualified name as it appears>",\n'
            '      "right_table": "<full qualified name as it appears>",\n'
            '      "join_type":   "INNER | LEFT | RIGHT | FULL | CROSS",\n'
            '      "condition":   "<the ON clause using real table names, NOT aliases -- e.g. Patient.PatientID = Referral.PatientID not a.PatientID = b.PatientID>"\n'
            "    }\n"
            "  ],\n"
            '  "filters":  ["<each WHERE condition as a string>"],\n'
            '  "grouping": ["<each GROUP BY expression as a string>"]\n'
            "}\n\n"
            "CRITICAL RULE — NO ALIASES:\n"
            "SQL aliases (like 'a', 'b', 'p', 't1') are shorthand used inside the SP, but they are "
            "meaningless outside its context. You MUST resolve every alias to the actual table name it "
            "refers to:\n"
            "  - In source_column: use TableName.ColumnName, never alias.ColumnName\n"
            "  - In condition: use real table names, never alias-prefixed columns\n"
            "  - Example: if the SP says 'SELECT a.Gender_ID FROM [Vault].Patient a', the "
            "source_column must be 'Gender_ID', NOT 'a.Gender_ID'\n\n"
            "STORED PROCEDURE:\n"
            "```sql\n%s\n```\n\n"
            "KNOWN TABLE CATALOGUE (for reference only -- use qualified names from SP, not these):\n%s"
        ) % (sp_code, catalogue_summary)

        prompt = "SYSTEM: %s\n\nUSER: %s" % (system_text, user_text)

        try:
            response = self.genai_client.models.generate_content(
                model=self.gemini_model,
                contents=prompt
            )
            text = response.text
        except Exception as e:
            logger.error("Gemini API error: %s", e)
            return None

        cleaned = _strip_llm_response_to_json(text)
        if cleaned is None:
            logger.error("Failed to extract JSON from Gemini response (empty or no JSON object found)")
            logger.debug("Gemini raw response: %s", text[:500])
            return None

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Gemini response as JSON: %s", e)
            logger.debug("Gemini response text: %s", text[:500])
            return None

    def _extract_with_nvidia(self, sp_code: str) -> Optional[Dict[str, Any]]:
        """Extract lineage using NVIDIA NIM (OpenAI-compatible API).
        Returns the parsed JSON dict or None."""
        if not self.nvidia_client:
            return None

        catalogue_summary = ""
        for table_name, info in self.catalogue.items():
            catalogue_summary += "- %s: %d columns\n" % (
                info.get("qualified_name", table_name),
                len(info.get("columns", [])),
            )

        system_prompt = (
            "You are an expert SQL data lineage analyst. "
            "Your job is to read a stored procedure and return ONLY valid JSON describing "
            "the data lineage -- no markdown, no explanation, just the JSON object."
        )

        user_prompt = (
            "Analyse this stored procedure. Return ONLY a JSON object "
            "(no markdown fences, no extra text) with EXACTLY this structure:\n\n"
            "{\n"
            '  "procedure_name": "<name of the SP>",\n'
            '  "target_table": "<the table written to -- use the full qualified name as it appears>",\n'
            '  "source_tables": ["<full qualified name as it appears>", ...],\n'
            '  "column_mappings": [\n'
            "    {\n"
            '      "target_column": "<column in target table>",\n'
            '      "source_table":  "<full qualified name as it appears>",\n'
            '      "source_column": "<actual column name, NOT the SQL alias -- e.g. Gender_ID not a.Gender_ID or p.Gender_ID>",\n'
            '      "transformation_type": "direct_copy | aggregation | calculation | conditional | constant",\n'
            '      "transformation_logic": "<brief plain-English description>"\n'
            "    }\n"
            "  ],\n"
            '  "joins": [\n'
            "    {\n"
            '      "left_table":  "<full qualified name as it appears>",\n'
            '      "right_table": "<full qualified name as it appears>",\n'
            '      "join_type":   "INNER | LEFT | RIGHT | FULL | CROSS",\n'
            '      "condition":   "<the ON clause using real table names, NOT aliases -- e.g. Patient.PatientID = Referral.PatientID not a.PatientID = b.PatientID>"\n'
            "    }\n"
            "  ],\n"
            '  "filters":  ["<each WHERE condition as a string>"],\n'
            '  "grouping": ["<each GROUP BY expression as a string>"]\n'
            "}\n\n"
            "CRITICAL RULE — NO ALIASES:\n"
            "SQL aliases (like 'a', 'b', 'p', 't1') are shorthand used inside the SP, but they are "
            "meaningless outside its context. You MUST resolve every alias to the actual table name it "
            "refers to:\n"
            "  - In source_column: use TableName.ColumnName, never alias.ColumnName\n"
            "  - In condition: use real table names, never alias-prefixed columns\n"
            "  - Example: if the SP says 'SELECT a.Gender_ID FROM [Vault].Patient a', the "
            "source_column must be 'Gender_ID', NOT 'a.Gender_ID'\n\n"
            "STORED PROCEDURE:\n"
            "```sql\n%s\n```\n\n"
            "KNOWN TABLE CATALOGUE (for reference only -- use qualified names from SP, not these):\n%s"
        ) % (sp_code, catalogue_summary)

        try:
            response = self.nvidia_client.chat.completions.create(
                model=self.model_nvidia,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=4096,
                temperature=0.2,
            )
            text = response.choices[0].message.content or ""
            logger.info("NVIDIA raw response length: %d chars", len(text))
            logger.debug("NVIDIA raw response (first 800 chars): %s", text[:800])
        except Exception as e:
            logger.error("NVIDIA NIM API error: %s", e)
            return None

        cleaned = _strip_llm_response_to_json(text)
        if cleaned is None:
            logger.error("Failed to extract JSON from NVIDIA response (empty or no JSON object found)")
            logger.info("NVIDIA response preview (first 500 chars): %s", text[:500])
            return None

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse NVIDIA response as JSON: %s", e)
            logger.info("NVIDIA cleaned text preview (first 500 chars): %s", cleaned[:500])
            return None

    def _log_failure(
        self,
        source_file: str,
        sp_code: str,
        step1_error: Optional[str],
        step2_error: Optional[str],
        step3_error: Optional[str],
        step4_error: Optional[str],
    ) -> None:
        """Append a structured failure record to the JSONL log."""
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_file": source_file,
            "failure_reason": "All attempted extraction steps failed",
            "step1_error": step1_error or "Unknown",
            "step2_error": step2_error or "Skipped or unknown",
            "step3_error": step3_error or "Skipped or unknown",
            "step4_error": step4_error or "Skipped or unknown",
            "sp_snippet": sp_code[:300],
        }
        try:
            with self.failure_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error("Failed to write to failure log: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    catalogue = {
        "CUSTOMERS": {
            "qualified_name": "[SalesDB].[dbo].[Customers]",
            "database": "SALESDB", "schema": "DBO",
            "columns": [{"name": "CUSTOMER_ID", "type": "INT", "nullable": False, "primary_key": True}],
            "source_file": "tables.sql"
        }
    }
    agent = SPAgent(
        sp_dir="data/sp",
        catalogue=catalogue,
        anthropic_api_key=None,
        gemini_api_key=None,
        nvidia_api_key=None,
        per_sp_overrides=load_sp_overrides(Path("data/sp_overrides.json")),
    )
    results = agent.run()
    print("Results: %s" % results)
