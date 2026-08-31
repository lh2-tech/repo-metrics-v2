#!/usr/bin/env python3
"""Merge several repo_metrics runs into one CSV.

Runs made at different times can carry different columns as the tool gains
fields. The union of all columns is emitted, with blanks where a run predates a
column, so old and new runs sit in one sheet without silently dropping data.

    python3 scripts/combine_runs.py \
        --run fablead=outputs/fablead-metrics \
        --run serpentcs-lh2=outputs/serpentcs-metrics \
        --out outputs/combined_repo_metrics.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
from pathlib import Path
from typing import Any, Dict, List

# Stable, meaningful ordering: identity, then the five spec metrics, then the
# figures that justify them, then diagnostics.
PREFERRED_ORDER = [
    "batch", "repo", "source_kind", "status",
    # the five headline metrics
    "logical_loc", "non_authored_loc", "duplication_ratio", "non_merge_commits",
    "build_test_status",
    # metric 2 audit trail
    "authored_loc", "non_authored_pct", "vendored_loc", "generated_minified_loc",
    # additive LOC view
    "code_loc", "data_loc", "code_loc_pct", "primary_code_language", "top_data_languages",
    # duplication detail
    "duplication_pct", "duplication_engine", "duplicated_lines",
    "duplication_scanned_lines", "duplication_clones",
    # commit detail
    "non_merge_commits_raw", "revert_commits_excluded", "contributors",
    "first_commit", "last_commit",
    # build detail
    "build_system", "build_systems_detected", "build_command", "test_command",
    "runtime_version", "runtime_version_source", "ci_runs_tests",
    "dockerfile_present", "build_check_mode",
    # context
    "platform", "analysis_branch", "default_branch", "primary_language",
    "language_breakdown", "total_files", "authored_files",
    "measurement_seconds", "error", "source",
]

DROP = {"repo_path", "repo_stats_excerpt", "build_log_tail", "duplication_command"}


def load(run_dir: Path, batch: str) -> List[Dict[str, Any]]:
    rows = []
    for path in sorted(glob.glob(str(run_dir / "artifacts" / "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        row["batch"] = batch
        # Runs predating --allow-non-git had no source_kind, but everything they
        # measured was a real git repo; mark inferred values rather than guessing
        # silently on rows that failed for unknown reasons.
        if not row.get("source_kind"):
            row["source_kind"] = "git" if row.get("status") == "ok" else ""
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", required=True,
                    help="batch=path/to/run-dir (repeatable)")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    rows: List[Dict[str, Any]] = []
    for spec in args.run:
        if "=" not in spec:
            print(f"bad --run {spec!r}, expected batch=path")
            return 2
        batch, _, path = spec.partition("=")
        found = load(Path(path), batch)
        print(f"  {batch:20} {len(found):>4} repos  <- {path}")
        rows.extend(found)

    if not rows:
        print("no rows found")
        return 2

    present = {k for r in rows for k in r} - DROP
    columns = [c for c in PREFERRED_ORDER if c in present]
    columns += sorted(present - set(columns))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (str(r.get("batch")), str(r.get("repo"))))
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})

    ok = [r for r in rows if r.get("status") == "ok"]
    print(f"\nwrote {args.out}")
    print(f"  {len(rows)} rows ({len(ok)} ok, {len(rows) - len(ok)} failed), {len(columns)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
