"""Data layer — discover, clean, filter, load, and aggregate session data."""

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Noise types to skip when cleaning transcripts
# ---------------------------------------------------------------------------
NOISE_TYPES = frozenset({
    "progress", "file-history-snapshot", "system", "queue-operation",
})


# ---------------------------------------------------------------------------
# Phase 1: Discover & Clean
# ---------------------------------------------------------------------------

def discover_sessions(sessions_dir, project_filter=None, since_days=None,
                      limit=None):
    """Find all session JSONL files, excluding subagent directories.

    Args:
        sessions_dir: Path to the Claude projects directory.
        project_filter: Substring filter for project directory names.
        since_days: Only include sessions modified within the last N days.
        limit: Maximum number of sessions to return (newest first).

    Returns:
        List of session dicts sorted by mtime descending.
    """
    sessions = []
    sessions_dir = Path(sessions_dir)

    if not sessions_dir.exists():
        print(f"Error: {sessions_dir} not found", file=sys.stderr)
        sys.exit(1)

    for project_dir in sorted(sessions_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name

        if project_filter and project_filter not in project_name:
            continue

        for jsonl in sorted(project_dir.glob("*.jsonl")):
            if "subagents" in str(jsonl):
                continue

            stat = jsonl.stat()
            mtime = stat.st_mtime
            size = stat.st_size

            if size < 100:
                continue

            if since_days is not None:
                cutoff = time.time() - (since_days * 86400)
                if mtime < cutoff:
                    continue

            sessions.append({
                "session_id": jsonl.stem,
                "project": project_name,
                "path": jsonl,
                "mtime": mtime,
                "size": size,
            })

    sessions.sort(key=lambda s: s["mtime"], reverse=True)

    if limit:
        sessions = sessions[:limit]

    return sessions


def clean_transcript(jsonl_path):
    """Extract clean text from a session JSONL.

    Returns:
        Tuple of (transcript_text, start_timestamp, end_timestamp).
    """
    lines = []
    timestamps = []
    errors = 0

    with open(jsonl_path, "r") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                errors += 1
                continue

            entry_type = entry.get("type", "")

            if entry_type in NOISE_TYPES:
                continue

            ts = entry.get("timestamp")
            if ts:
                timestamps.append(ts)

            if entry_type == "summary":
                summary = entry.get("summary", "")
                if summary:
                    lines.append(f"[SUMMARY] {summary}")
                continue

            if entry_type in ("user", "assistant"):
                msg = entry.get("message", {})
                content = msg.get("content", "")
                role = msg.get("role", entry_type)

                if isinstance(content, str) and content.strip():
                    text = content.strip()
                    if len(text) > 20_000:
                        text = text[:20_000] + "\n[...truncated...]"
                    lines.append(f"[{role.upper()}] {text}")
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "").strip()
                            if text:
                                if len(text) > 20_000:
                                    text = text[:20_000] + "\n[...truncated...]"
                                lines.append(f"[{role.upper()}] {text}")

    transcript = "\n".join(lines)
    start_ts = min(timestamps) if timestamps else None
    end_ts = max(timestamps) if timestamps else None

    if errors > 0 and len(lines) == 0:
        return "", start_ts, end_ts

    return transcript, start_ts, end_ts


def filter_cached(sessions, facets_dir, force=False):
    """Return sessions that need (re)processing based on mtime cache.

    Args:
        sessions: List of session dicts from discover_sessions().
        facets_dir: Path to the facets cache directory.
        force: If True, reprocess all sessions.

    Returns:
        List of session dicts that need processing.
    """
    if force:
        return sessions

    facets_dir = Path(facets_dir)
    to_process = []
    for s in sessions:
        facet_path = facets_dir / f"{s['session_id']}.json"
        if facet_path.exists():
            try:
                facet = json.loads(facet_path.read_text())
                cached_mtime = facet.get("_source_mtime", 0)
                if cached_mtime == s["mtime"]:
                    continue
            except (json.JSONDecodeError, KeyError):
                pass
        to_process.append(s)

    return to_process


# ---------------------------------------------------------------------------
# Phase 3: Load & Aggregate
# ---------------------------------------------------------------------------

def load_all_facets(facets_dir, project_filter=None, since_days=None):
    """Load all cached facets, optionally filtered.

    Args:
        facets_dir: Path to the facets cache directory.
        project_filter: Substring filter on facet project name.
        since_days: Only include facets with start_timestamp within N days.

    Returns:
        List of facet dicts.
    """
    facets = []
    facets_dir = Path(facets_dir)
    if not facets_dir.exists():
        return facets

    cutoff_dt = None
    if since_days is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=since_days)

    for fp in sorted(facets_dir.glob("*.json")):
        try:
            facet = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if project_filter and project_filter not in facet.get("project", ""):
            continue

        if cutoff_dt:
            ts = facet.get("start_timestamp")
            if ts:
                try:
                    facet_dt = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    )
                    if facet_dt < cutoff_dt:
                        continue
                except (ValueError, TypeError):
                    pass  # keep facets with unparseable timestamps

        facets.append(facet)
    return facets


def compute_aggregate_stats(facets):
    """Compute aggregate statistics from all facets.

    Returns:
        Dict with total_sessions, goal_categories, outcomes, helpfulness,
        session_types, friction_types, sessions_with_friction, projects.
    """
    stats = {
        "total_sessions": len(facets),
        "goal_categories": {},
        "outcomes": {},
        "helpfulness": {},
        "session_types": {},
        "friction_types": {},
        "sessions_with_friction": 0,
        "projects": {},
    }

    for f in facets:
        for cat, count in f.get("goal_categories", {}).items():
            stats["goal_categories"][cat] = (
                stats["goal_categories"].get(cat, 0) + count
            )

        outcome = f.get("outcome", "unclear")
        stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1

        h = f.get("claude_helpfulness", "unknown")
        stats["helpfulness"][h] = stats["helpfulness"].get(h, 0) + 1

        st = f.get("session_type", "unknown")
        stats["session_types"][st] = stats["session_types"].get(st, 0) + 1

        friction = f.get("friction_counts", {})
        if friction:
            stats["sessions_with_friction"] += 1
        for ft, count in friction.items():
            stats["friction_types"][ft] = (
                stats["friction_types"].get(ft, 0) + count
            )

        proj = f.get("project", "unknown")
        if proj not in stats["projects"]:
            stats["projects"][proj] = {
                "count": 0,
                "outcomes": {},
                "goal_categories": {},
                "friction_count": 0,
            }
        ps = stats["projects"][proj]
        ps["count"] += 1
        ps["outcomes"][outcome] = ps["outcomes"].get(outcome, 0) + 1
        for cat, count in f.get("goal_categories", {}).items():
            ps["goal_categories"][cat] = (
                ps["goal_categories"].get(cat, 0) + count
            )
        if friction:
            ps["friction_count"] += 1

    return stats


def compute_temporal_stats(facets):
    """Group facets by ISO week, return structured temporal data.

    Returns:
        List of dicts with keys: week, count, success_rate, active_projects.
        Sorted chronologically.
    """
    weekly = defaultdict(lambda: {
        "count": 0, "fully_achieved": 0, "projects": set(),
    })

    for f in facets:
        ts = f.get("start_timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        week_key = dt.strftime("%G-W%V")
        weekly[week_key]["count"] += 1
        if f.get("outcome") == "fully_achieved":
            weekly[week_key]["fully_achieved"] += 1
        weekly[week_key]["projects"].add(f.get("project", "unknown"))

    result = []
    for k, v in sorted(weekly.items()):
        success_rate = round(v["fully_achieved"] / v["count"] * 100) if v["count"] else 0
        result.append({
            "week": k,
            "count": v["count"],
            "success_rate": success_rate,
            "active_projects": len(v["projects"]),
        })

    return result


def demangle_project_name(encoded_name):
    """Convert encoded project directory name to human-readable form.

    Claude Code encodes project paths like:
        -Users-yulong-code-dotfiles -> dotfiles
        -Users-yulong-code-papers-sandbagging-detection -> papers/sandbagging-detection
    """
    parts = encoded_name.split("-")

    # Find the code/projects/writing directory marker
    markers = {"code", "projects", "writing", "scratch"}
    for i, part in enumerate(parts):
        if part.lower() in markers:
            remainder = parts[i + 1:]
            if remainder:
                return "/".join(remainder)

    # Fallback: use last component
    if parts:
        return parts[-1] or encoded_name
    return encoded_name


def list_projects(facets_dir):
    """List all unique projects with session counts.

    Args:
        facets_dir: Path to the facets cache directory.

    Returns:
        List of (human_name, encoded_name, count) tuples sorted by count desc.
    """
    facets = load_all_facets(facets_dir)
    project_counts = defaultdict(int)
    for f in facets:
        project_counts[f.get("project", "unknown")] += 1

    result = []
    for encoded, count in project_counts.items():
        human = demangle_project_name(encoded)
        result.append((human, encoded, count))

    result.sort(key=lambda x: x[2], reverse=True)
    return result
