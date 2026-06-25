"""ProcessingTracker — records which SP and schema files have been processed."""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ProcessingTracker:
    """
    Persists a record of which files have been successfully processed so
    subsequent runs can skip them and only handle new / unprocessed files.

    The tracker file is a JSON document with this shape::

        {
          "sp": {
            "Vault.Loadpatient.sql": {
              "status": "success",
              "method": "gemini",
              "processed_at": "2026-06-23T14:30:00Z",
              "target_table": "Vault.Patient"
            }
          },
          "schemas": {
            "tables.sql": {
              "status": "success",
              "tables_found": 47,
              "processed_at": "2026-06-23T14:30:00Z"
            }
          }
        }
    """

    def __init__(self, tracker_path: Path):
        self.path = Path(tracker_path)
        self.data: Dict[str, Dict[str, Any]] = self._load()

    # ── I/O ────────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load the tracker file from disk, or return an empty structure."""
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("sp", {})
                    data.setdefault("schemas", {})
                    return data
            except Exception as e:
                logger.warning("Failed to read tracker file %s: %s — starting fresh", self.path, e)
        return {"sp": {}, "schemas": {}}

    def save(self) -> None:
        """Write the current tracker state to disk."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            logger.debug("Saved tracker to %s", self.path)
        except Exception as e:
            logger.error("Failed to write tracker file %s: %s", self.path, e)

    # ── SP helpers ──────────────────────────────────────────────────────

    def is_sp_processed(self, filename: str) -> bool:
        """Return True if the SP file was previously processed successfully."""
        return self.data.get("sp", {}).get(filename, {}).get("status") == "success"

    def mark_sp(
        self,
        filename: str,
        status: str,
        method: str = "",
        target_table: str = "",
    ) -> None:
        """Record the processing outcome for an SP file."""
        entry: Dict[str, Any] = {
            "status": status,
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if method:
            entry["method"] = method
        if target_table:
            entry["target_table"] = target_table
        self.data.setdefault("sp", {})[filename] = entry

    # ── Schema helpers ──────────────────────────────────────────────────

    def is_schema_processed(self, filename: str) -> bool:
        """Return True if the schema file was previously processed successfully."""
        return self.data.get("schemas", {}).get(filename, {}).get("status") == "success"

    def mark_schema(
        self,
        filename: str,
        status: str,
        tables_found: int = 0,
    ) -> None:
        """Record the processing outcome for a schema file."""
        entry: Dict[str, Any] = {
            "status": status,
            "tables_found": tables_found,
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.data.setdefault("schemas", {})[filename] = entry

    # ── Summary / reset ─────────────────────────────────────────────────

    def processed_counts(self) -> Dict[str, Any]:
        """Return a summary of tracked state for the API."""
        sp_entries = self.data.get("sp", {})
        schema_entries = self.data.get("schemas", {})

        sp_success = sum(1 for v in sp_entries.values() if v.get("status") == "success")
        sp_failed = sum(1 for v in sp_entries.values() if v.get("status") == "failed")
        schema_success = sum(1 for v in schema_entries.values() if v.get("status") == "success")

        return {
            "sp_processed": sp_success,
            "sp_failed": sp_failed,
            "sp_tracked": len(sp_entries),
            "schemas_processed": schema_success,
            "schemas_tracked": len(schema_entries),
            "sp_files": dict(sp_entries),
            "schema_files": dict(schema_entries),
        }

    def reset(self) -> None:
        """Clear all tracking data (in memory + on disk)."""
        self.data = {"sp": {}, "schemas": {}}
        self.save()
        logger.info("Tracker reset — all processing records cleared")
