# SQL Data Lineage Explorer

A Python-based, AI-powered tool for extracting and visualizing data lineage from SQL stored procedures and schema definitions.

## Features

- **Three-step extraction pipeline**: Regex → Claude AI → Google Gemini (with fallback)
- **Interactive visualization**: Force-directed graph using D3.js v7
- **Column-level lineage**: See exactly how each column is derived
- **Multiple SQL dialect support**: Handles various table naming conventions
- **Failure logging**: Manual review of failed extractions
- **Caching**: Avoid reprocessing unchanged files
- **RESTful API**: Programmatic access to lineage data

## Installation

1. **Clone or download** this repository to your local machine.

2. **Create a virtual environment** (recommended):
   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment**:
   - **Windows (PowerShell)**:
     ```powershell
     .\venv\Scripts\activate
     ```
   - **Linux/Mac**:
     ```bash
     source venv/bin/activate
     ```

4. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

5. **(Optional) Set API keys** for enhanced extraction:
   - Get an [Anthropic API key](https://console.anthropic.com/) for Claude
   - Get a [Google API key](https://makersuite.google.com/app/apikey) for Gemini
   - Set them as environment variables:
     ```bash
     # Windows (PowerShell)
     $env:ANTHROPIC_API_KEY="your_anthropic_key_here"
     $env:GEMINI_API_KEY="your_gemini_key_here"

     # Linux/Mac
     export ANTHROPIC_API_KEY="your_anthropic_key_here"
     export GEMINI_API_KEY="your_gemini_key_here"
     ```

## Usage

### Running the Application

Start the Flask server:
```bash
python run.py
```

Optional arguments:
- `--preload`: Run the analysis pipeline at startup instead of on first request
- `--port`: Specify TCP port (default: 5000)

Example:
```bash
python run.py --preload --port 8080
```

### Accessing the Interface

Open your web browser to:
```
http://localhost:5000
```

### API Endpoints

- `POST /api/analyse`: Trigger analysis (JSON: `{"force": true}`)
- `GET /api/tables`: List all table names
- `GET /api/lineage/<table>`: Get lineage subgraph for a table
- `GET /api/column/<table>`: Get column lineage for a table
- `GET /api/stats`: Get statistics about the lineage graph
- `GET /api/full`: Get the full lineage graph
- `GET /api/failures`: Get list of failed extractions

## Project Structure

```
.
├── lineage_app/
│   ├── agents/
│   │   ├── __init__.py          # Table normalization utilities
│   │   ├── schema_agent.py       # Extracts table schemas from SQL files
│   │   ├── sp_parser.py          # Regex-based stored procedure parser
│   │   ├── sp_agent.py           # Coordinates 3-step extraction pipeline
│   │   └── lineage_agent.py      # Builds and queries the lineage graph
│   ├── templates/
│   │   └── index.html            # Interactive UI with D3.js visualization
│   ├── app.py                    # Flask application with all routes
│   └── run.py                    # Entry point with CLI arguments
├── data/
│   ├── sp/                       # Store stored procedure SQL files here
│   └── schemas/                  # Store schema SQL files here
├── lineage_cache.json            # Cached results (auto-generated)
├── logs/
│   └── failed_extractions.jsonl  # Failed extractions for manual review
├── venv/                         # Virtual environment (created during setup)
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

## How It Works

1. **Schema Agent**: Parses `CREATE TABLE` statements to build a table catalogue
2. **SP Agent**: Processes stored procedures through a three-step pipeline:
   - Step 1: Regex/sqlparse extraction (always runs)
   - Step 2: Claude AI extraction (if ANTHROPIC_API_KEY is set)
   - Step 3: Gemini AI extraction (if GEMINI_API_KEY is set)
   - Results are normalized and failures are logged
3. **Lineage Agent**: Builds a directed graph using NetworkX and provides query methods
4. **Flask App**: Serves the UI and API endpoints
5. **UI**: Interactive D3.js visualization with search, depth control, and column expansion panels

## Table Name Normalization

The tool normalizes table names to a standard format for comparison:
- Removes square brackets `[]` and double quotes `""`
- Removes database and schema prefixes (e.g., `[MyDB].[dbo].[Customers]` → `CUSTOMERS`)
- Converts to uppercase for consistent matching

## Supported SQL Features

- Target table detection: `INSERT INTO`, `SELECT INTO`, `UPDATE`, `MERGE`
- Source table detection: `FROM` and `JOIN` clauses
- Column mapping with alias resolution (e.g., `c.customer_id`)
- Transformation classification: direct copy, aggregation, calculation, conditional, constant
- JOIN extraction (type and condition)
- WHERE clause condition extraction
- GROUP BY expression extraction

## Troubleshooting

- **No tables showing**: Check that SQL files are in the correct directories (`data/sp/` and `data/schemas/`)
- **Failed extractions**: View failures via the UI or `GET /api/failures` endpoint
- **Port already in use**: Specify a different port with `--port`
- **Module not found**: 
  1. Ensure the virtual environment is activated (see Installation steps above)
  2. Ensure dependencies are installed with `pip install -r requirements.txt`
  3. If you encounter a `ModuleNotFoundError` for `werkzeug.routing.map`, try recreating the virtual environment and reinstalling dependencies.

## License

This project is open source and available under the MIT License.