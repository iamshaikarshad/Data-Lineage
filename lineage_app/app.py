"""Flask application for the SQL Data Lineage Explorer."""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

# Load .env file early
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent / '.env'
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # dotenv is optional; env vars may be set externally

from flask import Flask, render_template, request, jsonify

from agents.schema_agent import SchemaAgent
from agents.sp_agent import SPAgent, load_sp_overrides
from agents.lineage_agent import LineageAgent

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Paths
BASE_DIR = Path(__file__).parent
SP_DIR = BASE_DIR / 'data' / 'sp'
SCHEMA_DIR = BASE_DIR / 'data' / 'schemas'
OVERRIDES_FILE = BASE_DIR / 'data' / 'sp_overrides.json'
CACHE_FILE = BASE_DIR / 'lineage_cache.json'
FAILURE_LOG = BASE_DIR / 'logs' / 'failed_extractions.jsonl'

# Global variable to hold the lineage agent
_lineage_agent: Optional[LineageAgent] = None
_catalogue: Optional[Dict[str, Any]] = None

# Extra overrides injected from the CLI (via --force-method). These take
# precedence over values in sp_overrides.json.
_cli_overrides: Dict[str, str] = {}


def set_cli_overrides(overrides: Dict[str, str]) -> None:
    """
    Store command-line override values so that run_agents() can merge them
    with the file-based overrides. Called once at startup from run.py.
    """
    global _cli_overrides
    _cli_overrides = overrides


def run_agents(force: bool = False) -> Optional[LineageAgent]:
    """
    Run the full extraction pipeline (or load from cache).

    Args:
        force: If True, bypass cache and re-run agents.

    Returns:
        LineageAgent instance or None if failed.
    """
    global _lineage_agent, _catalogue

    # If we have a cached agent and not forcing, return it
    if not force and _lineage_agent is not None:
        logger.info("Returning cached lineage agent")
        return _lineage_agent

    # Try to load from cache unless forcing
    if not force:
        cached = load_from_cache()
        if cached is not None:
            _lineage_agent = cached
            return _lineage_agent

    # Run the agents
    logger.info("Running agents (force=%s)", force)

    # Ensure directories exist
    SP_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Run SchemaAgent
    logger.info("Running SchemaAgent")
    schema_agent = SchemaAgent(SCHEMA_DIR)
    catalogue = schema_agent.run()
    _catalogue = catalogue

    # Load per-SP overrides: file-based, then CLI overrides on top
    overrides = load_sp_overrides(OVERRIDES_FILE)
    if _cli_overrides:
        overrides.update(_cli_overrides)
        logger.info("Merged %d CLI override(s) into per-SP overrides", len(_cli_overrides))
    if overrides:
        logger.info("Loaded %d per-SP override(s) from %s + CLI", len(overrides), OVERRIDES_FILE)

    # Step 2: Run SPAgent
    logger.info("Running SPAgent")
    sp_agent = SPAgent(
        sp_dir=SP_DIR,
        catalogue=catalogue,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY"),
        nvidia_api_key=os.environ.get("NVIDIA_API_KEY"),
        per_sp_overrides=overrides,
    )
    sp_results = sp_agent.run()

    # Step 3: Normalise and run LineageAgent
    logger.info("Running LineageAgent")
    normalised_results = sp_agent.normalised_results()
    lineage_agent = LineageAgent(normalised_results)

    # Enrich nodes with catalogue data
    lineage_agent.enrich_nodes_from_catalogue(catalogue)

    # Cache the results
    _lineage_agent = lineage_agent
    cache_data = {
        "sp_results": sp_results,
        "catalogue": catalogue,
    }
    try:
        with CACHE_FILE.open("w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
        logger.info("Cached lineage data to %s", CACHE_FILE)
    except Exception as e:
        logger.error("Failed to write cache: %s", e)

    return lineage_agent


def load_from_cache() -> Optional[LineageAgent]:
    """
    Load lineage agent from cache file.

    Returns:
        LineageAgent instance or None if cache is invalid or missing.
    """
    global _catalogue

    if not CACHE_FILE.exists():
        logger.info("No cache file found at %s", CACHE_FILE)
        return None

    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            cache_data = json.load(f)
    except Exception as e:
        logger.error("Failed to read cache file: %s", e)
        return None

    # Validate cache data
    if not isinstance(cache_data, dict) or "sp_results" not in cache_data or "catalogue" not in cache_data:
        logger.error("Invalid cache data format")
        return None

    sp_results = cache_data["sp_results"]
    catalogue = cache_data["catalogue"]
    _catalogue = catalogue

    # Reconstruct the lineage agent
    try:
        lineage_agent = LineageAgent(sp_results)
        lineage_agent.enrich_nodes_from_catalogue(catalogue)
        logger.info("Loaded lineage agent from cache")
        return lineage_agent
    except Exception as e:
        logger.error("Failed to reconstruct lineage agent from cache: %s", e)
        return None


def _get_agent() -> Optional[LineageAgent]:
    """
    Get the lineage agent, loading from cache if necessary.

    Returns:
        LineageAgent instance or None if not available.
    """
    global _lineage_agent
    if _lineage_agent is None:
        _lineage_agent = load_from_cache()
    return _lineage_agent


@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')


@app.route('/api/analyse', methods=['POST'])
def analyse():
    """Run (or re-run) the analysis pipeline."""
    try:
        data = request.get_json() or {}
        force = data.get('force', False)
        agent = run_agents(force=force)
        if agent is None:
            return jsonify({"status": "error", "message": "Failed to run agents"}), 500
        stats = agent.statistics()
        return jsonify({"status": "ok", "statistics": stats})
    except Exception as e:
        logger.exception("Error in /api/analyse")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/tables')
def tables():
    """Return list of all table names."""
    agent = _get_agent()
    if agent is None:
        return jsonify({"tables": []})
    tables = agent.all_tables()
    return jsonify({"tables": tables})


@app.route('/api/lineage/<table>')
def lineage(table):
    """Return lineage subgraph for a table."""
    try:
        depth = int(request.args.get('depth', 5))
        direction = request.args.get('direction', 'both')
        agent = _get_agent()
        if agent is None:
            return jsonify({"error": "No lineage data available"}), 404

        if direction == 'upstream':
            subgraph = agent.get_upstream(table, depth)
        elif direction == 'downstream':
            subgraph = agent.get_downstream(table, depth)
        else:  # 'both'
            subgraph = agent.get_lineage(table, depth)

        if subgraph.number_of_nodes() == 0:
            return jsonify({"error": "Table '%s' not found" % table}), 404

        data = agent.to_serialisable(subgraph)
        return jsonify(data)
    except ValueError:
        return jsonify({"error": "Invalid depth parameter"}), 400
    except Exception as e:
        logger.exception("Error in /api/lineage/<table>")
        return jsonify({"error": str(e)}), 500


@app.route('/api/column/<table>')
def column_lineage(table):
    """Return column lineage for a table."""
    try:
        agent = _get_agent()
        if agent is None:
            return jsonify({"error": "No lineage data available"}), 404

        data = agent.column_lineage(table)
        return jsonify(data)
    except Exception as e:
        logger.exception("Error in /api/column/<table>")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stats')
def stats():
    """Return statistics about the lineage graph."""
    agent = _get_agent()
    if agent is None:
        return jsonify({
            "total_tables": 0,
            "total_edges": 0,
            "source_tables": 0,
            "target_tables": 0,
            "procedures": 0,
            "by_method": {"regex": 0, "claude": 0, "gemini": 0, "nvidia": 0, "failed": 0}
        })
    stats = agent.statistics()
    return jsonify(stats)


@app.route('/api/full')
def full_graph():
    """Return the full lineage graph."""
    agent = _get_agent()
    if agent is None:
        return jsonify({"error": "No lineage data available"}), 404
    data = agent.to_serialisable()
    return jsonify(data)


@app.route('/api/failures')
def failures():
    """Return the list of failed extractions."""
    if not FAILURE_LOG.exists():
        return jsonify({"failures": []})

    try:
        failures = []
        with FAILURE_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    failures.append(json.loads(line))
        return jsonify({"failures": failures})
    except Exception as e:
        logger.error("Failed to read failure log: %s", e)
        return jsonify({"failures": []})


# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    logger.exception("Internal server error")
    return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    app.run(debug=False, threaded=True, port=5000)
