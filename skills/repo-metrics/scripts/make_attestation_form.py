#!/usr/bin/env python3
"""Build a vendor attestation form for metric 5 from a repo_metrics run.

Metric 5 ("Builds and Tests") is sourced from vendor attestation, not
measurement — the runtime version in particular is frequently absent from the
repository itself, so no tool can extract it. This script pre-fills everything
that *was* detectable, leaving the vendor to confirm or correct rather than
research each repository from scratch.

    python3 scripts/make_attestation_form.py --run outputs/run1 \
        --out outputs/run1/attestation_form.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, List

FORM_COLUMNS = [
    "Repository",
    "Primary language",
    "Detected build system",
    "Build command (confirm or correct)",
    "Test command (confirm or correct)",
    "Runtime version (REQUIRED)",
    "Build & test status",
    "Notes / blockers",
    "Attested by",
    "Date",
]

STATUS_OPTIONS = "Builds + test suite passes | Builds only | Not verified"


def load_run(run_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(str(run_dir / "artifacts" / "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                rows.append(json.load(handle))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def to_form_row(metrics: Dict[str, Any]) -> Dict[str, str]:
    detected_status = str(metrics.get("build_test_status") or "Not verified")
    # Anything we did not actually execute stays blank for the vendor to fill,
    # so a detection gap is never mistaken for a verified negative.
    status = detected_status if metrics.get("build_ok") is True else ""

    notes = []
    if not metrics.get("runtime_version"):
        notes.append("no runtime pinned in repo")
    if str(metrics.get("build_system")) == "unknown":
        notes.append("no build manifest found")
    if metrics.get("ci_runs_tests") is False:
        notes.append("no CI test config")

    return {
        "Repository": str(metrics.get("repo") or ""),
        # primary_code_language ignores data/markup, so a sitemap-heavy repo does
        # not present to the vendor as an "XML" project.
        "Primary language": str(metrics.get("primary_code_language")
                                or metrics.get("primary_language") or ""),
        "Detected build system": str(metrics.get("build_system") or ""),
        "Build command (confirm or correct)": str(metrics.get("build_command") or ""),
        "Test command (confirm or correct)": str(metrics.get("test_command") or ""),
        "Runtime version (REQUIRED)": str(metrics.get("runtime_version") or ""),
        "Build & test status": status,
        "Notes / blockers": "; ".join(notes),
        "Attested by": "",
        "Date": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, type=Path,
                        help="a repo_metrics output directory (contains artifacts/)")
    parser.add_argument("--out", type=Path, help="output CSV (default: <run>/attestation_form.csv)")
    args = parser.parse_args()

    rows = load_run(args.run)
    if not rows:
        print(f"no artifacts found under {args.run}/artifacts")
        return 2

    out_path = args.out or (args.run / "attestation_form.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    form_rows = [to_form_row(r) for r in sorted(rows, key=lambda r: str(r.get("repo") or ""))]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORM_COLUMNS)
        writer.writeheader()
        writer.writerows(form_rows)

    missing_runtime = sum(1 for r in form_rows if not r["Runtime version (REQUIRED)"])
    missing_build = sum(1 for r in form_rows if not r["Build command (confirm or correct)"])
    prefilled = len(form_rows) - missing_build

    print(f"wrote {out_path}")
    print(f"  repositories:            {len(form_rows)}")
    print(f"  build command pre-filled:{prefilled:>4} / {len(form_rows)}")
    print(f"  runtime version blank:   {missing_runtime:>4} / {len(form_rows)}  <- vendor must supply")
    print(f"  status values allowed:   {STATUS_OPTIONS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
