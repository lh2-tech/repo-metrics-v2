#!/usr/bin/env python3
"""One run: due-diligence metrics AND a first-N% commit-history sample, per repo.

Combines two existing, independently-tested scripts by composition — neither
is reimplemented, both are imported and called as-is:

  * repo_metrics.py      the 5-metric scorecard (LOC, non-authored LOC,
                         duplication, commits, build/test)
  * make_sample_repo.py  slices a repo down to its first N% of commit history

Every CLI flag repo_metrics.py accepts still works here (same parser,
extended) — this is a superset, not a rewrite. Run it exactly like
repo_metrics.py, add `--pct` and friends for the sampling half.

    python3 run_metrics_and_sample.py --repos repos.txt --out run1

Output layout (under <out>/, i.e. outputs/repo-metrics/<out>/):
    repo_metrics.csv, repo_metrics.jsonl, summary.json, artifacts/*.json
        -- unchanged repo_metrics.py output, byte-for-byte the same as
           running repo_metrics.py alone with the same flags.
    samples/<repo>/                  -- the sliced sample repo (skipped repos
                                         have no directory here, see below)
    samples/<repo>.zip                -- only with --zip-samples
    sample_report.csv, sample_report.jsonl
        -- one row per repo: status (ok/skipped/error), reason, commit
           counts, the cutoff SHA, and whether `git fsck` came back clean.
    run_report.json
        -- one combined summary: totals for both halves, and a count of how
           often each fallback below actually fired this run.

FALLBACK LADDER (each one is real, each one is logged when it fires, and
none of them ever aborts the batch — a bad repo is recorded and the run
moves on):

  duplication      jscpd -> repo_metrics.py's own built-in token-shingling
                   detector if jscpd/node isn't on PATH. Rows are labelled
                   duplication_engine=builtin-fallback; those numbers are
                   NOT comparable to jscpd's.
  build/test       --build-check docker -> detect (static identification
                   only) if the docker daemon is unreachable. Unchanged
                   repo_metrics.py behavior.
  sample source    Reuses the SAME clone repo_metrics.py just made -> a
                   fresh full re-clone from origin if that clone is
                   partial/blobless (repo_metrics.py's default is
                   --filter=blob:none, so THIS FALLBACK FIRES FOR EVERY
                   REMOTE REPO UNLESS YOU PASS --full-clone). It is slower
                   (a second network fetch) but automatic and correct —
                   partial clones cannot be gc'd/pruned locally at all.
  sample gc        --aggressive (smaller output) -> plain `gc --prune=now`
                   if aggressive repacking exceeds --aggressive-timeout.
                   Both fully drop unreachable history; only compression
                   differs. This is what stopped a real repo with heavy
                   binary history from hanging for 20+ minutes.
  too few commits  Sampling is SKIPPED (not an error) for any repo with
                   fewer commits than --min-commits (default 20) on the
                   sampled branch — a percentage slice of a short history
                   isn't meaningful. Metrics still run normally.
  metrics vs sample  A crash in one half never blocks or corrupts the
                   other for the same repo — they're independent try/except
                   domains, both recorded either way.
  scc missing      NOT recoverable — the whole run stops with exit code 2,
                   same as repo_metrics.py alone. Nothing can be measured
                   without it.

Concurrency: one thread pool, --workers wide, runs metrics THEN sampling for
each repo (sequentially per repo, parallel across repos) — this is what lets
sampling reuse the clone metrics just made without a race.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import repo_metrics  # noqa: E402
import make_sample_repo as mkr  # noqa: E402
from workspace_paths import resolve_out  # noqa: E402


def log(msg: str) -> None:
    # Local time, matching repo_metrics.py's own log() — the two interleave
    # in the same run, and a mismatched clock reads as a bug when it isn't.
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── sampling half, reusing make_sample_repo's own tested functions ──────────

SAMPLE_REPORT_COLUMNS = [
    "repo", "platform", "sample_status", "reason", "branch_sampled",
    "total_commits", "cutoff_commit_index", "cutoff_commit_sha",
    "reachable_commits_in_sample", "fsck_clean", "sample_path", "sample_zip_path",
    "elapsed_seconds",
]


def resolve_metrics_clone_path(spec: repo_metrics.RepoSpec, cfg: argparse.Namespace) -> Optional[Path]:
    """Where repo_metrics.py just cloned (or would have cloned) this repo.

    Deterministic and cheap — mirrors ensure_clone()'s own destination
    logic without re-invoking it, so this never triggers a second clone by
    itself. If metrics failed before cloning (bad auth, repo not found), the
    path simply won't exist and sampling records that as its skip reason.
    """
    if spec.local_path is not None:
        return spec.local_path
    return Path(cfg.clones_dir) / spec.platform / spec.name


def sample_one(spec: repo_metrics.RepoSpec, cfg: argparse.Namespace, tokens: Dict[str, str],
               samples_root: Path, work_root: Path) -> Dict[str, Any]:
    started = time.time()
    row: Dict[str, Any] = {
        "repo": spec.full_name, "platform": spec.platform, "sample_status": "error",
        "reason": "", "branch_sampled": "", "total_commits": "", "cutoff_commit_index": "",
        "cutoff_commit_sha": "", "reachable_commits_in_sample": "", "fsck_clean": "",
        "sample_path": "", "sample_zip_path": "",
    }
    work_dir = work_root / spec.name
    out_path = samples_root / spec.name
    try:
        clone_path = resolve_metrics_clone_path(spec, cfg)
        if clone_path is None or not (clone_path / ".git").exists():
            row["sample_status"] = "skipped"
            row["reason"] = "no local clone available (metrics failed before/without cloning)"
            return row

        # Feed make_sample_repo the SAME clone repo_metrics.py just produced,
        # wrapped as a local RepoSpec — this is what lets it detect a
        # partial/blobless clone and fall back to a fresh full re-clone
        # automatically, using its own already-tested logic unchanged.
        sample_spec = repo_metrics.RepoSpec(spec.raw, spec.platform, spec.full_name, spec.clone_url, clone_path)

        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        src = mkr.materialise_source(sample_spec, tokens, work_dir, cfg.clone_timeout)
        branch = cfg.sample_branch or mkr.resolve_default_branch(src)
        row["branch_sampled"] = branch

        try:
            cutoff_sha, total, cutoff_idx = mkr.cutoff_commit(src, branch, cfg.pct, cfg.min_commits)
        except mkr.SampleError as exc:
            row["sample_status"] = "skipped"
            row["reason"] = str(exc)
            row["total_commits"] = mkr.commit_count(src, branch)
            return row

        row["total_commits"] = total
        row["cutoff_commit_index"] = cutoff_idx
        row["cutoff_commit_sha"] = cutoff_sha

        sample_branch_name = cfg.sample_branch_name or f"sample{cfg.pct:g}pct".replace(".", "_")
        mkr.truncate_to(src, cutoff_sha, sample_branch_name)
        mkr.prune(src, cfg.aggressive, cfg.aggressive_timeout)

        verify = mkr.verify(src, sample_branch_name)
        row["reachable_commits_in_sample"] = verify["reachable_commits"]
        row["fsck_clean"] = verify["fsck_clean"]

        if out_path.exists():
            shutil.rmtree(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(out_path))

        manifest = {
            "source": spec.raw, "platform": spec.platform, "branch_sampled": branch,
            "sample_branch_name": sample_branch_name, "requested_pct": cfg.pct,
            "total_commits": total, "cutoff_commit_index": cutoff_idx,
            "cutoff_commit_sha": cutoff_sha,
            "reachable_commits_in_sample": verify["reachable_commits"],
            "fsck_clean": verify["fsck_clean"], "aggressive_gc": cfg.aggressive,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }
        (out_path / "SAMPLE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        row["sample_path"] = str(out_path)
        row["sample_status"] = "ok"

        if cfg.zip_samples:
            zip_base = str(out_path)
            shutil.make_archive(zip_base, "zip", root_dir=out_path.parent, base_dir=out_path.name)
            row["sample_zip_path"] = f"{zip_base}.zip"

        return row

    except mkr.SampleError as exc:
        row["reason"] = mkr.redact(str(exc), tokens)
        return row
    except subprocess.TimeoutExpired as exc:
        row["reason"] = f"timed out: {exc}"
        return row
    except Exception as exc:  # noqa: BLE001 - a sample crash must never break the batch
        row["reason"] = f"{type(exc).__name__}: {exc}"
        return row
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        row["elapsed_seconds"] = round(time.time() - started, 1)


def write_sample_report(rows: List[Dict[str, Any]], out_dir: Path) -> None:
    csv_path = out_dir / "sample_report.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: str(r.get("repo") or "")):
            writer.writerow({k: row.get(k, "") for k in SAMPLE_REPORT_COLUMNS})
    with (out_dir / "sample_report.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


# ── combined cli ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    # Superset of repo_metrics.py's own parser — every metrics flag still
    # works exactly as documented there; these are the sampling additions.
    parser = repo_metrics.build_parser()
    parser.description = __doc__
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    sampling = parser.add_argument_group("sampling (make_sample_repo.py)")
    sampling.add_argument("--pct", type=float, default=5.0,
                           help="percentage of commit history to keep in each sample (default 5)")
    sampling.add_argument("--sample-branch", default=None,
                           help="branch to sample (default: each repo's own default branch)")
    sampling.add_argument("--sample-branch-name", default=None,
                           help="name for the truncated branch (default: sample<pct>pct)")
    sampling.add_argument("--min-commits", type=int, default=20,
                           help="skip sampling (not an error) below this many commits (default 20)")
    sampling.add_argument("--aggressive", action="store_true",
                           help="smaller sample output via 'git gc --aggressive'; can be slow on "
                                "repos with large binary history — bounded by --aggressive-timeout, "
                                "auto-falls-back to a plain prune on timeout")
    sampling.add_argument("--aggressive-timeout", type=int, default=300)
    sampling.add_argument("--zip-samples", action="store_true", help="also write samples/<repo>.zip")
    sampling.add_argument("--skip-sampling", action="store_true",
                           help="metrics only — run exactly as repo_metrics.py would alone")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    cfg = build_parser().parse_args(argv)

    # ── metrics half: unchanged repo_metrics.py setup and per-repo pipeline ──
    cfg.exclude_dirs = [d.strip() for d in cfg.exclude_dirs.split(",") if d.strip()]
    cfg.data_languages = [d.strip() for d in cfg.data_languages.split(",") if d.strip()]
    if cfg.wide_exclude:
        cfg.exclude_dirs = list(dict.fromkeys(cfg.exclude_dirs + repo_metrics.WIDE_EXTRA_EXCLUDE_DIRS))

    scc_bin = repo_metrics.find_scc()
    if not scc_bin:
        log("ERROR: scc not found. Set SCC_BIN, or place it at .tools/bin/scc, or brew install scc")
        log("This is not recoverable — nothing can be measured without it.")
        return 2

    jscpd_cmd: Optional[List[str]] = None
    dup_engine = "builtin-fallback"
    if not cfg.no_jscpd:
        jscpd_cmd, dup_engine = repo_metrics.find_jscpd()
    fallbacks_fired = Counter()
    if jscpd_cmd is None and not cfg.no_jscpd:
        log("FALLBACK: jscpd not found (needs Node) -> using the built-in duplication detector. "
            "Rows will read duplication_engine=builtin-fallback; not comparable to jscpd numbers.")
        fallbacks_fired["duplication_builtin"] += 1

    if cfg.build_check == "docker" and not repo_metrics.docker_available():
        log("FALLBACK: docker daemon unreachable -> downgrading --build-check to detect")
        cfg.build_check = "detect"
        fallbacks_fired["build_check_detect"] += 1

    if not cfg.full_clone and not cfg.skip_sampling:
        log("NOTE: metrics clones are partial (--filter=blob:none) by default, so sampling will "
            "fall back to a fresh full re-clone from origin for every REMOTE repo below — this is "
            "correct but costs a second network fetch per repo. Pass --full-clone to avoid it.")

    specs = repo_metrics.load_repo_list(cfg.repos, cfg.default_platform, cfg.gitlab_host)
    if cfg.limit:
        specs = specs[: cfg.limit]
    if not specs:
        log(f"ERROR: no usable repo entries in {cfg.repos}")
        return 2

    out_dir: Path = resolve_out(cfg.out)
    (out_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    Path(cfg.clones_dir).mkdir(parents=True, exist_ok=True)
    tokens = repo_metrics.load_tokens(cfg.tokens_file)

    samples_root = out_dir / "samples"
    work_root = out_dir / ".sample_work"
    if not cfg.skip_sampling:
        if work_root.exists():
            shutil.rmtree(work_root)
        samples_root.mkdir(parents=True, exist_ok=True)

    metrics_rows: List[Dict[str, Any]] = []
    sample_rows: List[Dict[str, Any]] = []
    pending: List[repo_metrics.RepoSpec] = []
    for spec in specs:
        artifact = out_dir / "artifacts" / f"{spec.name}.json"
        if artifact.is_file() and not cfg.force:
            try:
                metrics_rows.append(json.loads(artifact.read_text(encoding="utf-8")))
                continue
            except json.JSONDecodeError:
                pass
        pending.append(spec)

    log(f"{len(specs)} repos | {len(metrics_rows)} cached | {len(pending)} to process | "
        f"scc={scc_bin} duplication={dup_engine} build-check={cfg.build_check} "
        f"sampling={'off' if cfg.skip_sampling else f'first {cfg.pct:g}%'} workers={cfg.workers}")

    def process_one(spec: repo_metrics.RepoSpec) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        metrics_row = repo_metrics.process_repo(spec, cfg, scc_bin, jscpd_cmd, dup_engine, tokens, out_dir)
        sample_row = None
        if not cfg.skip_sampling:
            sample_row = sample_one(spec, cfg, tokens, samples_root, work_root)
        return metrics_row, sample_row

    done = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = {pool.submit(process_one, spec): spec for spec in pending}
            for future in concurrent.futures.as_completed(futures):
                spec = futures[future]
                done += 1
                try:
                    metrics_row, sample_row = future.result()
                except Exception as exc:  # noqa: BLE001 - never lose the whole run
                    metrics_row = {"repo": spec.full_name, "platform": spec.platform, "source": spec.raw,
                                   "status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    sample_row = None

                metrics_rows.append(metrics_row)
                m_flag = "OK " if metrics_row.get("status") == "ok" else "ERR"
                s_flag = ""
                if sample_row is not None:
                    sample_rows.append(sample_row)
                    tag = {"ok": "OK", "skipped": "SKIP", "error": "ERR"}.get(sample_row["sample_status"], "?")
                    s_flag = f" sample={tag}"

                log(f"[{done}/{len(pending)}] {m_flag}{s_flag} {metrics_row.get('repo')} "
                    f"loc={metrics_row.get('logical_loc', '-')} dup={metrics_row.get('duplication_ratio', '-')} "
                    f"commits={metrics_row.get('non_merge_commits', '-')} "
                    f"({metrics_row.get('measurement_seconds', '?')}s)")

                repo_metrics.write_outputs(metrics_rows, out_dir)
                if sample_rows:
                    write_sample_report(sample_rows, out_dir)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    repo_metrics.write_outputs(metrics_rows, out_dir)
    if sample_rows:
        write_sample_report(sample_rows, out_dir)

    sample_status_counts = dict(Counter(r["sample_status"] for r in sample_rows))
    run_report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "repos_total": len(specs),
        "metrics_ok": sum(1 for r in metrics_rows if r.get("status") == "ok"),
        "metrics_failed": sum(1 for r in metrics_rows if r.get("status") != "ok"),
        "sampling_enabled": not cfg.skip_sampling,
        "sample_status_counts": sample_status_counts,
        "fallbacks_fired_setup": dict(fallbacks_fired),
        "requested_pct": cfg.pct,
        "full_clone_mode": cfg.full_clone,
    }
    (out_dir / "run_report.json").write_text(json.dumps(run_report, indent=2), encoding="utf-8")
    log(f"run_report: {json.dumps(run_report)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
