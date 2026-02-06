"""CLI entry point — argparse, main loop, report opening."""

import argparse
import platform
import subprocess
import sys
import time
from pathlib import Path

from claude_insights.gemini import (
    check_gemini_cli,
    generate_report,
    make_batches,
    process_batch,
    save_facet,
)
from claude_insights.sessions import (
    clean_transcript,
    discover_sessions,
    filter_cached,
    list_projects,
    load_all_facets,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_OUTPUT_DIR = Path.home() / ".claude" / "custom-insights"


def open_report(path):
    """Open the report in the default browser."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", str(path)], check=False)
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser(
        prog="claude-insights",
        description=(
            "Claude Code Usage Insights — analyze your sessions, "
            "extract patterns, generate coaching reports."
        ),
    )
    parser.add_argument(
        "--project",
        help="Substring filter for project name",
    )
    parser.add_argument(
        "--since", type=int,
        help="Only sessions newer than N days",
    )
    parser.add_argument(
        "--limit", type=int,
        help="Max sessions to process",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate all facets (ignore cache)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show plan without calling Gemini",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Regenerate report from cached facets",
    )
    parser.add_argument(
        "--list-projects", action="store_true",
        help="List available projects with session counts",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Detailed progress output",
    )
    parser.add_argument(
        "--sessions-dir", type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help=f"Path to Claude projects directory (default: {DEFAULT_SESSIONS_DIR})",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Path to output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    facets_dir = args.output_dir / "facets"
    prompts_dir = Path(__file__).parent / "prompts"

    # --list-projects mode
    if args.list_projects:
        projects = list_projects(facets_dir)
        if not projects:
            print("No cached facets found. Run extraction first.")
            sys.exit(1)
        print(f"{'Project':<40} {'Sessions':>8}")
        print("-" * 50)
        for human, encoded, count in projects:
            print(f"{human:<40} {count:>8}")
        return

    # --report-only mode
    if args.report_only:
        facets = load_all_facets(
            facets_dir,
            project_filter=args.project,
            since_days=args.since,
        )
        if not facets:
            print(
                "No cached facets found. Run without --report-only first.",
                file=sys.stderr,
            )
            sys.exit(1)

        label = f" (filtered: {args.project})" if args.project else ""
        print(f"Loaded {len(facets)} cached facets{label}")

        report_path = generate_report(
            facets, prompts_dir, args.output_dir,
            verbose=args.verbose, project_slug=args.project,
        )
        if report_path:
            print(f"\nReport: {report_path}")
            open_report(report_path)
        return

    # Full pipeline: check Gemini CLI
    check_gemini_cli()

    # Phase 1: Discover
    print("Phase 1: Discovering sessions...")
    sessions = discover_sessions(
        args.sessions_dir,
        project_filter=args.project,
        since_days=args.since,
        limit=args.limit,
    )
    n_projects = len(set(s["project"] for s in sessions))
    print(f"  Found {len(sessions)} sessions across {n_projects} projects")

    if not sessions:
        print("No sessions to process.")
        return

    # Filter to uncached
    to_process = filter_cached(sessions, facets_dir, force=args.force)
    cached_count = len(sessions) - len(to_process)
    print(f"  {cached_count} already cached, {len(to_process)} to process")

    if not to_process and not args.force:
        print("\nAll sessions cached. Regenerating report...")
        facets = load_all_facets(
            facets_dir, project_filter=args.project, since_days=args.since,
        )
        report_path = generate_report(
            facets, prompts_dir, args.output_dir,
            verbose=args.verbose, project_slug=args.project,
        )
        if report_path:
            print(f"\nReport: {report_path}")
            open_report(report_path)
        return

    # Extract transcripts
    print("\nExtracting transcripts...")
    items = []
    empty_count = 0
    for s in to_process:
        transcript, start_ts, end_ts = clean_transcript(s["path"])
        if not transcript.strip():
            empty_count += 1
            continue
        items.append({
            **s,
            "transcript": transcript,
            "start_ts": start_ts,
            "end_ts": end_ts,
        })

    total_chars = sum(len(item["transcript"]) for item in items)
    print(
        f"  Extracted {len(items)} transcripts "
        f"({total_chars // 1000}K chars total)"
    )
    if empty_count:
        print(f"  Skipped {empty_count} empty sessions")

    if not items:
        print("No transcripts to process.")
        return

    # Phase 2: Batch and process
    batches = make_batches(items)
    print(
        f"\nPhase 2: Processing {len(items)} sessions "
        f"in {len(batches)} batches"
    )

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for i, batch in enumerate(batches, 1):
            chars = sum(len(item["transcript"]) for item in batch)
            print(f"  Batch {i}: {len(batch)} sessions, {chars // 1000}K chars")
            if args.verbose:
                for item in batch:
                    print(
                        f"    - {item['session_id'][:12]}... "
                        f"({len(item['transcript']) // 1000}K chars, "
                        f"{item['project']})"
                    )
        return

    # Load facet prompt
    facet_prompt = (prompts_dir / "facet_prompt.txt").read_text()

    total_facets = 0
    start_time = time.time()

    for i, batch in enumerate(batches, 1):
        results = process_batch(
            batch, facet_prompt, i, len(batches), verbose=args.verbose,
        )
        for session_id, facet in results:
            save_facet(session_id, facet, facets_dir)
            total_facets += 1

    elapsed = time.time() - start_time
    print(f"\nPhase 2 complete: {total_facets} facets in {elapsed:.0f}s")

    # Phase 3: Generate report
    print("\nPhase 3: Generating report...")
    facets = load_all_facets(
        facets_dir, project_filter=args.project, since_days=args.since,
    )
    print(f"  Total facets (cached + new): {len(facets)}")

    report_path = generate_report(
        facets, prompts_dir, args.output_dir,
        verbose=args.verbose, project_slug=args.project,
    )
    if report_path:
        print(f"\nReport: {report_path}")
        open_report(report_path)

    print("\nDone!")
