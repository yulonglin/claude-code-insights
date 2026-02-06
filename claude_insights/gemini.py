"""LLM layer — Gemini CLI calls, batching, facet parsing, report generation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_insights.sessions import compute_aggregate_stats, compute_temporal_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BATCH_SIZE = 12
BATCH_CHAR_LIMIT = 700_000
MAX_RETRIES = 3
RETRY_BACKOFF = [30, 60, 120]


# ---------------------------------------------------------------------------
# Gemini CLI interface
# ---------------------------------------------------------------------------

def check_gemini_cli():
    """Verify Gemini CLI is installed. Exit with helpful message if not."""
    if shutil.which("gemini") is None:
        print(
            "Error: Gemini CLI not found.\n\n"
            "Install it with:\n"
            "  npm install -g @anthropic-ai/gemini-cli\n"
            "  # or\n"
            "  brew install gemini-cli\n\n"
            "Then authenticate:\n"
            "  gemini\n\n"
            "See: https://github.com/google-gemini/gemini-cli",
            file=sys.stderr,
        )
        sys.exit(1)


def call_gemini(prompt_text):
    """Call Gemini CLI via temp file to avoid stdin pipe limits.

    Returns:
        Tuple of (response_envelope, error_string).
        On success, error is None. On failure, envelope is None.
    """
    tmp_dir = tempfile.gettempdir()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=tmp_dir,
    ) as f:
        f.write(prompt_text)
        tmp_path = f.name

    try:
        result = subprocess.run(
            f'cat "{tmp_path}" | gemini -m gemini-2.5-pro -p "" -o json',
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            stderr_snippet = (result.stderr[:500] if result.stderr
                              else "(no stderr)")
            return None, f"Exit code {result.returncode}: {stderr_snippet}"

        stdout = result.stdout.strip()
        if not stdout:
            return None, "Empty stdout"

        envelope = json.loads(stdout)
        return envelope, None

    except subprocess.TimeoutExpired:
        return None, "Timeout (300s)"
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Phase 2: Batch processing
# ---------------------------------------------------------------------------

def make_batches(sessions_with_transcripts):
    """Group sessions into batches respecting size and count limits."""
    batches = []
    current_batch = []
    current_chars = 0

    for item in sessions_with_transcripts:
        item_chars = len(item["transcript"])

        if item_chars > 200_000:
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
            batches.append([item])
            continue

        if (len(current_batch) >= BATCH_SIZE
                or current_chars + item_chars > BATCH_CHAR_LIMIT):
            if current_batch:
                batches.append(current_batch)
            current_batch = [item]
            current_chars = item_chars
        else:
            current_batch.append(item)
            current_chars += item_chars

    if current_batch:
        batches.append(current_batch)

    return batches


def build_batch_prompt(batch, facet_prompt):
    """Assemble the prompt for a batch of sessions."""
    parts = [facet_prompt, "\n\n"]
    for item in batch:
        parts.append(f"===SESSION_BOUNDARY::{item['session_id']}===\n")
        parts.append(item["transcript"])
        parts.append("\n\n")
    return "".join(parts)


def parse_facets_response(response_text, expected_count):
    """Parse facets from Gemini's response string.

    Returns:
        Tuple of (facets_list, error_string).
    """
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed, None
        elif isinstance(parsed, dict):
            return [parsed], None
        else:
            return None, f"Unexpected type: {type(parsed)}"
    except json.JSONDecodeError:
        # Fallback: extract individual JSON objects
        facets = []
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start:i + 1])
                        facets.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = None

        if facets:
            return facets, None
        return None, "Could not parse any JSON objects from response"


def process_batch(batch, facet_prompt, batch_idx, total_batches, verbose=False):
    """Process a single batch through Gemini.

    Returns:
        List of (session_id, facet) tuples.
    """
    batch_chars = sum(len(item["transcript"]) for item in batch)
    n = len(batch)
    print(
        f"  [Batch {batch_idx}/{total_batches}] "
        f"Processing {n} sessions ({batch_chars // 1000}K chars)...",
        end="", flush=True,
    )

    prompt = build_batch_prompt(batch, facet_prompt)
    session_ids = [item["session_id"] for item in batch]
    session_map = {item["session_id"]: item for item in batch}

    for attempt in range(MAX_RETRIES):
        envelope, error = call_gemini(prompt)
        if error:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            print(f" error: {error}", flush=True)
            if attempt < MAX_RETRIES - 1:
                print(
                    f"    Retrying in {wait}s "
                    f"(attempt {attempt + 2}/{MAX_RETRIES})...",
                    end="", flush=True,
                )
                time.sleep(wait)
                continue
            print(f"    FAILED after {MAX_RETRIES} attempts", flush=True)
            return []

        response_text = envelope.get("response", "")
        facets, parse_error = parse_facets_response(response_text, n)

        if parse_error:
            print(f" parse error: {parse_error}", flush=True)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                print(f"    Retrying in {wait}s...", end="", flush=True)
                time.sleep(wait)
                continue
            print(
                f"    FAILED to parse after {MAX_RETRIES} attempts",
                flush=True,
            )
            return []

        if len(facets) != n:
            print(
                f" count mismatch: got {len(facets)}, expected {n}",
                flush=True,
            )
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                print(f"    Retrying in {wait}s...", end="", flush=True)
                time.sleep(wait)
                continue
            print(
                f"    Using {len(facets)} of {n} (partial)", flush=True,
            )

        # Match facets to sessions
        results = []
        matched_ids = set()
        for facet in facets:
            fid = facet.get("session_id", "")
            if fid in session_map:
                item = session_map[fid]
                facet["project"] = item["project"]
                facet["start_timestamp"] = item.get("start_ts")
                facet["end_timestamp"] = item.get("end_ts")
                facet["_source_mtime"] = item["mtime"]
                results.append((fid, facet))
                matched_ids.add(fid)

        unmatched = set(session_ids) - matched_ids
        if unmatched and verbose:
            print(f"    Unmatched session IDs: {unmatched}", flush=True)

        elapsed = (
            envelope.get("stats", {})
            .get("models", {})
            .get("gemini-2.5-pro", {})
            .get("api", {})
            .get("totalLatencyMs", 0)
        )
        print(f" done ({elapsed // 1000}s, {len(results)} facets)", flush=True)
        return results

    return []


def save_facet(session_id, facet, facets_dir):
    """Save a facet to the cache directory."""
    facets_dir = Path(facets_dir)
    facets_dir.mkdir(parents=True, exist_ok=True)
    facet_path = facets_dir / f"{session_id}.json"
    facet_path.write_text(json.dumps(facet, indent=2))


# ---------------------------------------------------------------------------
# Phase 3: Report generation
# ---------------------------------------------------------------------------

def generate_report(facets, prompts_dir, output_dir, verbose=False,
                    project_slug=None):
    """Generate HTML report by feeding stats + facets to Gemini.

    Args:
        facets: List of facet dicts.
        prompts_dir: Path to the prompts directory.
        output_dir: Path to the output directory.
        verbose: Enable verbose output.
        project_slug: If set, tailor report to this project.

    Returns:
        Path to the generated report, or None on error.
    """
    stats = compute_aggregate_stats(facets)
    temporal = compute_temporal_stats(facets)

    report_prompt = (Path(prompts_dir) / "report_prompt.txt").read_text()

    if project_slug:
        report_prompt += (
            "\n\nNOTE: These facets are filtered to a single project. "
            "Tailor the report specifically to this project rather than "
            "cross-project comparisons.\n"
        )

    # Build compact facet summaries
    compact_facets = []
    for f in facets:
        summary = {
            "session_id": f.get("session_id"),
            "project": f.get("project"),
            "underlying_goal": f.get("underlying_goal"),
            "outcome": f.get("outcome"),
            "claude_helpfulness": f.get("claude_helpfulness"),
            "session_type": f.get("session_type"),
            "goal_categories": f.get("goal_categories"),
            "friction_counts": f.get("friction_counts"),
            "friction_detail": f.get("friction_detail"),
            "primary_success": f.get("primary_success"),
            "improvement_opportunity": f.get("improvement_opportunity", ""),
            "start_timestamp": f.get("start_timestamp"),
            "end_timestamp": f.get("end_timestamp"),
        }
        compact_facets.append(
            {k: v for k, v in summary.items() if v}
        )

    input_text = (
        f"{report_prompt}\n\n"
        f"## AGGREGATE STATS\n```json\n"
        f"{json.dumps(stats, indent=2)}\n```\n\n"
        f"## TEMPORAL DATA\n```json\n"
        f"{json.dumps(temporal, indent=2)}\n```\n\n"
        f"## ALL FACETS ({len(compact_facets)} sessions)\n"
        f"```json\n"
        f"{json.dumps(compact_facets, separators=(',', ':'))}\n```\n"
    )

    input_chars = len(input_text)
    print(
        f"\nGenerating report ({input_chars // 1000}K chars input)...",
        flush=True,
    )

    envelope, error = call_gemini(input_text)
    if error:
        print(f"Error generating report: {error}", file=sys.stderr)
        return None

    html = envelope.get("response", "")

    # Strip markdown fences if present
    if html.startswith("```"):
        lines = html.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        html = "\n".join(lines)

    # Timestamped output with symlink
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if project_slug:
        slug = project_slug.replace("/", "-").replace(" ", "-").lower()
        report_name = f"report_{slug}_{ts}.html"
    else:
        report_name = f"report_{ts}.html"

    report_path = output_dir / report_name
    report_path.write_text(html)

    # Update latest symlink
    latest = output_dir / "report_latest.html"
    latest.unlink(missing_ok=True)
    latest.symlink_to(report_path.name)

    return report_path
