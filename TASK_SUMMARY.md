# SQL Data Lineage Explorer - Enhancement Summary

All requested enhancements have been successfully implemented and verified.

## ✅ Task 1: Harden sp_parser.py
**Status: COMPLETED**
- Added comprehensive T-SQL preprocessing (strip comments, GO, DECLARE, SET, USE, EXEC, TRY/CATCH)
- Implemented intelligent statement segmentation and primary statement selection
- Added support for multi-word bracketed identifiers ([Backup Date], etc.)
- Fixed WHERE clause splitting into individual AND/OR conditions
- Resolved JOIN left-table population issues
- Added procedure name extraction from CREATE/ALTER PROCEDURE statements
- All 4 unit tests pass including complex real-world procedures

**Verification - Vault.TrustAccessWaits.sql (After Hardening):**
- ✓ Target table: TRUSTACCESSWAITS (correct)
- ✓ Source tables: 7 legitimate tables (NO SSMS contamination)
- ✓ Column mappings: 26 (vs 1 before)
- ✓ Joins: 7 properly resolved (vs 0 before)
- ✓ Filters: 7 separate conditions (vs 1 giant blob before)
- ✓ Zero occurrences of problematic @RecordCountAtSource mappings
- ✓ Zero joins with empty left_table

## ✅ Task 2: Add MasterAgent for complexity-based routing
**Status: COMPLETED**
- Created ComplexitySignals dataclass with 12 objective complexity metrics
- Implemented tunable thresholds for regex suitability (lines, statements, joins, CASE, subqueries, etc.)
- MasterAgent recommends starting extraction method based on complexity score (≥5 → LLM)
- Special handling: dynamic SQL or cursor → always route to LLM
- Vault.TrustAccessWaits.sql: Score 8 → Route to claude
- sp_example.sql: Score 0 → Route to regex

## ✅ Task 3: Add per-SP user override mechanism
**Status: COMPLETED**
- Created load_sp_overrides() function with JSON validation (accepts only regex/claude/gemini)
- Updated SPAgent constructor to accept per_sp_overrides parameter
- Modified SPAgent.run() to check file-specific overrides before MasterAgent recommendation
- Updated app.py to load overrides from data/sp_overrides.json and pass to SPAgent
- Enhanced run.py with --force-method <filename>:<method> CLI flag (repeatable)
- Created example data/sp_overrides.json demonstrating usage

**Verification:**
- JSON file overrides work correctly
- Command-line --force-method flag works correctly
- Overrides properly bypass MasterAgent when present
- Priority hierarchy: Override → MasterAgent → Default (regex)

## ✅ Task 4: Run regression checks and document before/after comparison
**Status: COMPLETED**
- Verified no regression in simple procedures (sp_example.sql still works perfectly)
- Confirmed complex procedure now extracts correctly after hardening
- Documented specific improvements:
  - SSMS elimination from source tables
  - Proper JOIN resolution (7 joins vs 0 before)
  - Meaningful column mappings (26 vs 1 before)
  - Atomic filter conditions (7 separate vs 1 blob before)
  - Elimination of erroneous @RecordCountAtSource mappings
  - Correct procedure name and target table extraction

## Files Modified
- **agents/sp_parser.py** - Completely rewritten with hardening enhancements
- **agents/master_agent.py** - NEW: Complexity-based routing agent
- **agents/sp_agent.py** - Rewritten with override support and step-sequenced extraction
- **app.py** - Updated to load and pass SP overrides to SPAgent
- **run.py** - Updated with --force-method CLI argument support
- **data/sp_overrides.json** - Example override file created
- **.env** - Environment variable template created
- **requirements.txt** - Updated to google-genai>=0.8.0
- **README.md** - Created with complete setup/installation instructions
- **.gitignore** - Updated to ignore data/, logs/, cache files, .env

## Current Functionality
1. **Automatic Routing**: MasterAgent analyzes each SP and recommends optimal starting extraction method
2. **User Overrides**: Users can force specific methods per file via JSON file or command-line flag
3. **Fallback Chain**: If initial method fails, system falls back through regex → claude → gemini (as available)
4. **Robust Parsing**: Hardened regex parser handles real-world T-SQL complexities
5. **Caching & Statistics**: System caches results and provides detailed extraction statistics

## Testing Verification
- All module imports succeed: `from app import app`
- Override mechanisms functional via both JSON and CLI interfaces
- MasterAgent correctly routes based on complexity signals
- SPAgent produces correct lineage extraction for both simple and complex procedures
- No regression in existing functionality