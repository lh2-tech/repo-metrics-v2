#!/usr/bin/env python3
"""Slice a repo down to its first N% of commit history, as a standalone sample repo.

Given any repo this workspace already knows how to reach (a local clone —
full or partial/blobless — or a github/gitlab/bitbucket URL), produce a new,
self-contained git repository whose entire reachable history is exactly the
oldest N% of commits on one branch. Everything after the cutoff, every other
branch, the reflog and the original remote are all dropped, so the result is
small, portable and safe to hand out (no tokens, no unrelated history).

Why this exists
----------------
Built after doing this by hand, repeatedly, via ad hoc shell one-liners that
broke on real-world repos in three distinct ways this script now handles:

  * A *partial (blobless) clone* — e.g. one produced by repo_metrics.py's
    default `--filter=blob:none` — cannot be `git gc --prune`d locally:
    gc/repack needs every historical blob, not just the ones a checkout
    happened to fetch on demand, and dies with "unable to read <sha>". Fix:
    detect `remote.origin.promisor = true` and re-clone fresh (full) from
    the origin URL instead of trying to complete the partial clone in place.
  * A working copy with uncommitted/untracked cruft aborts `git checkout`
    with "local changes would be overwritten". Fix: `reset --hard` +
    `clean -fdx` on the *copy* before ever switching commits (never touches
    the source).
  * `git gc --aggressive` on a repo with large binary history (media/game
    assets) can run for 20+ minutes doing a maximal delta search for a few
    hundred KB of savings. Fix: plain `gc --prune=now` by default (still
    fully drops unreachable objects — that's the correctness property this
    script cares about), `--aggressive` is opt-in and time-bounded, and
    falls back to the plain prune if it doesn't finish in time.

Usage
-----
    # local clone (full or partial — detected automatically)
    python3 make_sample_repo.py --repo /path/to/clone --pct 5 --out sample/

    # remote, using this workspace's token conventions
    python3 make_sample_repo.py --repo https://github.com/org/repo --pct 5 --out sample/

    # zip the result too
    python3 make_sample_repo.py --repo owner/repo --pct 5 --out sample/ --zip

Exit codes: 0 success, 1 bad input / repo unusable, 2 git operation failed.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Reuse this workspace's own URL parsing / token / auth logic rather than
# re-implementing it — one definition of "how to reach a repo" per invariant.
from repo_metrics import (  # noqa: E402
    RepoSpec,
    authed_url,
    load_tokens,
    parse_repo_entry,
)
from workspace_paths import resolve_out  # noqa: E402


class SampleError(RuntimeError):
    """A handled, reported failure — as opposed to a bug in this script."""


def log(msg: str) -> None:
    # Local time, matching repo_metrics.py's own log() convention — the two
    # interleave when run together via run_metrics_and_sample.py.
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 900,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise SampleError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def redact(text: str, tokens: dict) -> str:
    for token in tokens.values():
        if token:
            text = text.replace(token, "***")
    return text


# ── source resolution ────────────────────────────────────────────────────────

def is_promisor_clone(repo: Path) -> bool:
    cfg = repo / ".git" / "config"
    if not cfg.is_file():
        return False
    return "promisor = true" in cfg.read_text(encoding="utf-8", errors="ignore")


def origin_url(repo: Path) -> Optional[str]:
    result = run(["git", "config", "--get", "remote.origin.url"], repo, check=False)
    url = result.stdout.strip()
    return url or None


def materialise_source(spec: RepoSpec, tokens: dict, work_dir: Path,
                        clone_timeout: int) -> Path:
    """Produce a *complete*, disposable working copy at work_dir/src.

    Local, non-partial clones are filesystem-copied (fast, no network).
    Everything else (a URL, or a local partial/blobless clone) gets a fresh
    full `git clone` — the only way to guarantee every historical blob is
    actually present, which a straight `cp` of a partial clone cannot.
    """
    dest = work_dir / "src"
    if dest.exists():
        shutil.rmtree(dest)

    if spec.local_path is not None:
        if not (spec.local_path / ".git").exists():
            raise SampleError(f"not a git repository: {spec.local_path}")
        if is_promisor_clone(spec.local_path):
            url = origin_url(spec.local_path)
            if not url:
                raise SampleError(
                    f"{spec.local_path} is a partial/blobless clone with no "
                    "origin remote configured — cannot complete it locally "
                    "and nowhere to re-clone it from."
                )
            log(f"source is a partial (blobless) clone; re-cloning fresh from origin ({redact(url, tokens)})")
            _clone_full(url, dest, tokens, clone_timeout, platform_hint=spec.platform)
        else:
            log(f"source is a full local clone; copying filesystem ({spec.local_path})")
            shutil.copytree(spec.local_path, dest, symlinks=True)
    else:
        if not spec.clone_url:
            raise SampleError(f"could not resolve a clone URL from: {spec.raw}")
        log(f"cloning fresh from {spec.platform}: {spec.full_name}")
        _clone_full(spec.clone_url, dest, tokens, clone_timeout, platform_hint=spec.platform)

    return dest


def _clone_full(url: str, dest: Path, tokens: dict, timeout: int, platform_hint: str) -> None:
    # A URL read back from an existing clone's `git config` (the promisor
    # path) may already carry credentials baked in by whatever cloned it
    # originally. Re-running it through authed_url() would inject a second
    # set, producing a malformed userinfo section — inject only when the
    # URL is actually bare.
    already_authed = bool(urlsplit(url).username)
    auth = url if already_authed else authed_url(url, platform_hint, tokens)
    try:
        run(["git", "clone", "--quiet", auth, str(dest)], timeout=timeout)
    except SampleError as exc:
        raise SampleError(redact(str(exc), tokens)) from None


# ── truncation ────────────────────────────────────────────────────────────────

def resolve_default_branch(repo: Path) -> str:
    head = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], repo, check=False)
    if head.stdout.strip():
        return head.stdout.strip()
    origin_head = run(
        ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], repo, check=False
    )
    if origin_head.stdout.strip():
        return origin_head.stdout.strip().split("/", 1)[-1]
    raise SampleError("could not determine a branch to sample (detached HEAD, no origin/HEAD)")


def commit_count(repo: Path, ref: str) -> int:
    return int(run(["git", "rev-list", "--count", ref], repo).stdout.strip())


def cutoff_commit(repo: Path, ref: str, pct: float, min_commits: int) -> tuple[str, int, int]:
    total = commit_count(repo, ref)
    if total < min_commits:
        raise SampleError(
            f"only {total} commits on {ref!r} — below --min-commits {min_commits}; "
            "a percentage slice of a history this short isn't meaningful. "
            "Lower --min-commits to override."
        )
    cutoff = max(1, math.ceil(total * pct / 100))
    shas = run(["git", "rev-list", "--reverse", ref], repo).stdout.splitlines()
    if cutoff > len(shas):
        cutoff = len(shas)
    return shas[cutoff - 1], total, cutoff


def truncate_to(dest: Path, cutoff_sha: str, sample_branch: str) -> None:
    # Never let leftover working-tree state from the source block the checkout.
    run(["git", "reset", "--hard", "--quiet"], dest)
    run(["git", "clean", "-fdx", "--quiet"], dest)

    run(["git", "checkout", "--quiet", "--force", cutoff_sha], dest)
    run(["git", "checkout", "--quiet", "-b", sample_branch], dest)

    branches = run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], dest
    ).stdout.split()
    for b in branches:
        if b != sample_branch:
            run(["git", "branch", "-D", b], dest, check=False)

    run(["git", "remote", "remove", "origin"], dest, check=False)
    remotes = run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes"], dest, check=False
    ).stdout.split()
    for r in remotes:
        run(["git", "update-ref", "-d", r], dest, check=False)

    run(["git", "reflog", "expire", "--expire=now", "--all"], dest)


def prune(dest: Path, aggressive: bool, aggressive_timeout: int) -> None:
    if aggressive:
        log(f"gc --aggressive (bounded to {aggressive_timeout}s; falls back to plain prune)")
        try:
            run(["git", "gc", "--prune=now", "--aggressive", "--quiet"], dest,
                timeout=aggressive_timeout)
            return
        except subprocess.TimeoutExpired:
            log("gc --aggressive exceeded its time budget — killing it and falling back")
        except SampleError as exc:
            log(f"gc --aggressive failed ({exc}); falling back to plain prune")
        # a partial/killed repack can leave a .tmp-*-pack file behind; gc
        # will happily ignore and retry, but clear it so nothing looks stuck
        for stray in (dest / ".git" / "objects" / "pack").glob(".tmp-*"):
            stray.unlink(missing_ok=True)

    run(["git", "gc", "--prune=now", "--quiet"], dest, timeout=max(aggressive_timeout, 900))


def verify(dest: Path, sample_branch: str) -> dict:
    fsck = run(["git", "fsck", "--full"], dest, check=False)
    reachable = int(run(["git", "rev-list", "--count", "HEAD"], dest).stdout.strip())
    return {
        "reachable_commits": reachable,
        "fsck_clean": fsck.returncode == 0 and not fsck.stdout.strip(),
        "fsck_output": fsck.stdout.strip()[:2000],
    }


# ── entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True,
                   help="local clone path, or a github/gitlab/bitbucket URL / owner-repo shorthand")
    p.add_argument("--pct", type=float, default=5.0, help="percentage of commit history to keep (default 5)")
    p.add_argument("--out", required=True, help="destination directory for the sample repo")
    p.add_argument("--branch", default=None, help="branch to sample (default: the repo's default branch)")
    p.add_argument("--sample-branch-name", default=None,
                   help="name for the truncated branch (default: sample<pct>pct)")
    p.add_argument("--min-commits", type=int, default=20,
                   help="refuse to sample a branch with fewer commits than this (default 20)")
    p.add_argument("--aggressive", action="store_true",
                   help="use 'git gc --aggressive' for smaller output (can be very slow on repos "
                        "with large binary history; auto-falls-back to a plain prune on timeout)")
    p.add_argument("--aggressive-timeout", type=int, default=300,
                   help="seconds to allow --aggressive before falling back (default 300)")
    p.add_argument("--clone-timeout", type=int, default=1800,
                   help="seconds to allow a fresh full clone (default 1800)")
    p.add_argument("--force", action="store_true", help="overwrite --out if it already exists")
    p.add_argument("--zip", action="store_true", help="also write <out>.zip alongside the sample repo")
    p.add_argument("--default-platform", choices=["github", "gitlab"], default="github",
                   help="platform assumed for a bare owner/repo shorthand (default github)")
    p.add_argument("--gitlab-host", default="gitlab.com")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    cfg = build_parser().parse_args(argv)

    out_dir = resolve_out(cfg.out)
    if out_dir.exists():
        if not cfg.force:
            log(f"ERROR: {out_dir} already exists — pass --force to overwrite")
            return 1
        shutil.rmtree(out_dir)
    work_root = out_dir.parent / f".make_sample_work_{out_dir.name}"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    spec = parse_repo_entry(cfg.repo, cfg.default_platform, cfg.gitlab_host)
    if spec is None:
        log(f"ERROR: could not parse --repo {cfg.repo!r}")
        return 1

    tokens = load_tokens(None)
    sample_branch = cfg.sample_branch_name or f"sample{cfg.pct:g}pct".replace(".", "_")

    try:
        started = time.time()
        src = materialise_source(spec, tokens, work_root, cfg.clone_timeout)

        branch = cfg.branch or resolve_default_branch(src)
        log(f"sampling branch {branch!r}")

        cutoff_sha, total, requested_cutoff = cutoff_commit(src, branch, cfg.pct, cfg.min_commits)
        log(f"total={total} commits, first {cfg.pct:g}% => commit #{requested_cutoff} ({cutoff_sha[:10]})")

        truncate_to(src, cutoff_sha, sample_branch)
        prune(src, cfg.aggressive, cfg.aggressive_timeout)

        report = verify(src, sample_branch)
        if not report["fsck_clean"]:
            log("WARNING: git fsck reported issues in the sample — inspect before relying on it:")
            log(report["fsck_output"])

        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(out_dir))

        manifest = {
            "source": spec.raw,
            "platform": spec.platform,
            "branch_sampled": branch,
            "sample_branch_name": sample_branch,
            "requested_pct": cfg.pct,
            "total_commits": total,
            "cutoff_commit_index": requested_cutoff,
            "cutoff_commit_sha": cutoff_sha,
            "reachable_commits_in_sample": report["reachable_commits"],
            "fsck_clean": report["fsck_clean"],
            "aggressive_gc": cfg.aggressive,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 1),
        }
        (out_dir / "SAMPLE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        size_mb = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file()) / 1_048_576
        log(f"done: {report['reachable_commits']} commits reachable, {size_mb:.1f} MB -> {out_dir}")

        if cfg.zip:
            zip_base = str(out_dir)
            log(f"zipping -> {zip_base}.zip")
            shutil.make_archive(zip_base, "zip", root_dir=out_dir.parent, base_dir=out_dir.name)
            log(f"wrote {zip_base}.zip")

        return 0

    except SampleError as exc:
        log(f"ERROR: {redact(str(exc), tokens)}")
        return 2
    except subprocess.TimeoutExpired as exc:
        log(f"ERROR: timed out: {exc}")
        return 2
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
