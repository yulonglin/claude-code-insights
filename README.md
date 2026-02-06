# Claude Code Insights

Analyze your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions — extract usage patterns and generate personalized coaching reports.

Uses [Gemini CLI](https://github.com/google-gemini/gemini-cli) to process session transcripts through a two-phase pipeline:

1. **Extract** — batch-process session transcripts into structured facets (cached per-session)
2. **Analyze** — aggregate facets into temporal stats
3. **Report** — generate a coaching-style HTML report with actionable insights

## Prerequisites

- **Python 3.9+** (stdlib only — zero pip dependencies)
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — install and authenticate:
  ```bash
  npm install -g @google/gemini-cli
  gemini  # authenticate on first run
  ```
- **Claude Code sessions** — the tool reads from `~/.claude/projects/` (Claude Code's default session storage)

## Quick Start

```bash
# Clone the repo
git clone https://github.com/yulonglin/claude-code-insights.git
cd claude-code-insights

# Run the full pipeline (extract + report)
python3 -m claude_insights

# Or just regenerate the report from cached facets
python3 -m claude_insights --report-only

# Filter to a specific project
python3 -m claude_insights --report-only --project dotfiles

# See all available projects
python3 -m claude_insights --list-projects
```

## Usage

```
usage: claude-insights [-h] [--project PROJECT] [--since SINCE]
                       [--limit LIMIT] [--force] [--dry-run] [--report-only]
                       [--list-projects] [--verbose]
                       [--sessions-dir SESSIONS_DIR] [--output-dir OUTPUT_DIR]

optional arguments:
  -h, --help            show this help message and exit
  --project PROJECT     Substring filter for project name
  --since SINCE         Only sessions newer than N days
  --limit LIMIT         Max sessions to process
  --force               Regenerate all facets (ignore cache)
  --dry-run             Show plan without calling Gemini
  --report-only         Regenerate report from cached facets
  --list-projects       List available projects with session counts
  --verbose             Detailed progress output
  --sessions-dir PATH   Path to Claude projects directory
                        (default: ~/.claude/projects)
  --output-dir PATH     Path to output directory
                        (default: ~/.claude/custom-insights)
```

## How It Works

### Phase 1: Facet Extraction

Sessions are discovered from `~/.claude/projects/*/`. Each session JSONL is cleaned (noise filtered, messages extracted, long content truncated) and batched for Gemini processing.

Gemini analyzes each session and produces a structured **facet** with:
- Goal categories (feature implementation, debugging, refactoring, etc.)
- Outcome (fully achieved, partially achieved, unclear, abandoned)
- Claude helpfulness rating
- Friction types (wrong approach, tool failure, context loss, etc.)
- Improvement opportunities

Facets are cached per-session with mtime-based invalidation — re-runs only process new or modified sessions.

### Phase 2: Aggregation

Pre-computed statistics include:
- Aggregate counts across all dimensions
- Per-project breakdowns
- **Temporal trends** — weekly session counts, success rates, and active project counts

### Phase 3: Report Generation

Gemini generates a self-contained HTML report with five sections:

1. **How You Use Claude Code** — mirrors your usage patterns back to you
2. **What Makes Your Usage Distinctive** — surfaces non-obvious patterns
3. **Temporal Trends** — weekly usage charts (CSS-only, no JavaScript)
4. **What's Working Well** — reinforces effective patterns with evidence
5. **What to Change** — honest critique with specific remedies, including CLAUDE.md improvement suggestions

Reports are timestamped (`report_YYYYMMDD_HHMMSS.html`) with a `report_latest.html` symlink.

## Output Files

```
~/.claude/custom-insights/
├── facets/                          # Individual session facets (JSON)
│   ├── <session-uuid>.json
│   └── ...
├── report_20260206_034800.html      # Timestamped reports
├── report_dotfiles_20260206_035200.html  # Project-filtered report
└── report_latest.html → report_...  # Symlink to most recent
```

## Examples

```bash
# First run — extracts all sessions, generates report
python3 -m claude_insights
# → Phase 1: Discovering sessions...
# →   Found 528 sessions across 23 projects
# →   0 already cached, 528 to process
# → Phase 2: Processing 528 sessions in 44 batches
# →   [Batch 1/44] Processing 12 sessions (680K chars)... done (45s, 12 facets)
# →   ...
# → Phase 3: Generating report...
# → Report: ~/.claude/custom-insights/report_20260206_034800.html

# Subsequent runs — only processes new sessions
python3 -m claude_insights
# →   528 already cached, 3 to process

# Project-specific report
python3 -m claude_insights --report-only --project dotfiles
# → Loaded 200 cached facets (filtered: dotfiles)

# Last 7 days only
python3 -m claude_insights --report-only --since 7

# Preview what would be processed
python3 -m claude_insights --dry-run --verbose
```

## Architecture

```
claude_insights/
├── cli.py         # argparse + main loop + report opening
├── sessions.py    # Data layer: discover, clean, filter, load, aggregate, temporal
├── gemini.py      # LLM layer: call Gemini, batch, parse, generate report
└── prompts/
    ├── facet_prompt.txt    # Extraction prompt (per-session analysis)
    └── report_prompt.txt   # Report prompt (coaching-style HTML generation)
```

Three modules, one responsibility each. A contributor can understand the full architecture by reading three files.

## License

MIT
