"""Entry point for the SQL Data Lineage Explorer application."""

import argparse
import logging
import os
import sys
from pathlib import Path

# Add the current directory to the path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent))

import app as flask_app

logger = logging.getLogger(__name__)


def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='SQL Data Lineage Explorer')
    parser.add_argument('--preload', action='store_true',
                        help='Run pipeline at startup instead of on first request')
    parser.add_argument('--port', type=int, default=5000,
                        help='TCP port to listen on (default: 5000)')
    parser.add_argument('--force-method', action='append', default=[],
                        help='Force extraction method for a specific SP file. '
                             'Format: <filename>:<method> where method is regex, '
                             'claude, gemini, or nvidia. Can be repeated.')
    args = parser.parse_args()

    setup_logging()

    # Process force-method overrides
    cli_overrides = {}
    if args.force_method:
        for override in args.force_method:
            if ':' not in override:
                logger.error("Invalid --force-method format: %s. Use <filename>:<method>", override)
                sys.exit(1)
            filename, method = override.split(':', 1)
            if method not in ('regex', 'claude', 'gemini', 'nvidia'):
                logger.error("Invalid method in --force-method: %s. Must be regex, claude, gemini, or nvidia", method)
                sys.exit(1)
            cli_overrides[filename] = method
            logger.info("Will force method %s for file %s", method, filename)

    # Push CLI overrides into app module so run_agents() can merge them
    if cli_overrides:
        flask_app.set_cli_overrides(cli_overrides)

    # Ensure required directories exist
    base_dir = Path(__file__).parent
    sp_dir = base_dir / 'data' / 'sp'
    schema_dir = base_dir / 'data' / 'schemas'
    logs_dir = base_dir / 'logs'

    for directory in [sp_dir, schema_dir, logs_dir]:
        directory.mkdir(parents=True, exist_ok=True)
        logger.info("Ensured directory exists: %s", directory)

    # Check for API keys and warn if missing
    anthro_key = os.environ.get("ANTHROPIC_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    nvidia_key = os.environ.get("NVIDIA_API_KEY")

    if not anthro_key and not gemini_key and not nvidia_key:
        logger.warning("No LLM API keys set -- only regex extraction will be attempted")
    if not anthro_key:
        logger.warning("ANTHROPIC_API_KEY not set -- Claude extraction step will be skipped")
    if not gemini_key:
        logger.warning("GEMINI_API_KEY not set -- Gemini extraction step will be skipped")
    if not nvidia_key:
        logger.warning("NVIDIA_API_KEY not set -- NVIDIA extraction step will be skipped")

    # Count SQL files
    sp_files = list(sp_dir.rglob("*.sql"))
    schema_files = list(schema_dir.rglob("*.sql"))
    logger.info("Found %d stored procedure file(s)", len(sp_files))
    logger.info("Found %d schema file(s)", len(schema_files))

    # Preload if requested
    if args.preload:
        logger.info("Preloading agents...")
        try:
            agent = flask_app.run_agents(force=True)
            if agent is not None:
                stats = agent.statistics()
                logger.info("Preload complete: %d tables, %d edges",
                            stats['total_tables'], stats['total_edges'])
            else:
                logger.error("Preload failed - no agent returned")
        except Exception as e:
            logger.exception("Failed to preload agents")
    else:
        logger.info("Agents will run on first UI trigger (use --preload to change this)")

    # Start the Flask application
    logger.info("Starting Flask server on port %d", args.port)
    flask_app.app.run(
        debug=False,
        threaded=True,
        host='0.0.0.0',
        port=args.port
    )


if __name__ == '__main__':
    main()
