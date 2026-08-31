# repo-metrics + sampling, combined package

One run produces both the due-diligence metrics report AND a first-N%
commit-history sample, for every repo in a list. Two ways to run it —
containerized or plain Python — both use the exact same script.

```
repo_metrics_docker_package/
├── datalabs_paths.py
├── .dockerignore
└── skills/repo-metrics/
    ├── scripts/
    │   ├── run_metrics_and_sample.py   <- run this one; combines the two below
    │   ├── repo_metrics.py             <- the 5-metric scorecard, unchanged
    │   ├── make_sample_repo.py         <- the sampling script, unchanged
    │   ├── workspace_paths.py          <- required import
    │   ├── make_attestation_form.py    <- optional: metric-5 vendor attestation form
    │   └── combine_runs.py             <- optional: merge multiple run outputs
    └── docker/
        ├── Dockerfile
        └── README.md                  <- full Docker build/run instructions
```

`run_metrics_and_sample.py` combines the other two **by composition** —
neither is reimplemented. Its own module docstring (`--help`, or read the
file directly) carries the authoritative fallback table; the copy below is
a summary.

## Option A — Docker

Quickest path: `docker-compose.yml` at this package's root bakes in the
three bind mounts (repo list, tokens, output dir) so you don't retype them
per run.

```bash
cd repo_metrics_docker_package
touch .env               # tokens optional for public repos; file just needs to exist
docker compose build
docker compose run --rm repo-metrics --repos /data/repos.txt --out run1 --pct 5
```

Everything after `repo-metrics` passes straight through to
`run_metrics_and_sample.py` — same flags as Option B below.

Need the raw `docker build`/`docker run` commands, or the local-repo-mount
or `--build-check docker` socket-mount variants? See
`skills/repo-metrics/docker/README.md` — `docker-compose.yml` is equivalent
to those, just pre-wired; the manual commands are still documented there.

## Option B — plain Python

Needs on PATH: `git`, `scc` (LOC counter — hard requirement, no fallback),
and optionally `node`+`jscpd` (duplication detection — falls back
automatically to a weaker built-in detector if absent). No pip packages
needed; the scripts are stdlib-only.

```bash
cd repo_metrics_docker_package
python3 skills/repo-metrics/scripts/run_metrics_and_sample.py \
    --repos repos.txt --out run1 --pct 5
```

Run this from the package root (not from inside `scripts/`) — `repos.txt`
lives here, and any bare local-folder entries in it (e.g. a repo checked
out as a sibling folder, just `mim-hr` rather than a path) are resolved
relative to wherever `repos.txt` itself lives, so this also works fine as
`python3 skills/repo-metrics/scripts/run_metrics_and_sample.py --repos
path/to/repos.txt --out run1 --pct 5` from any cwd.

Tokens (`GH_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN` /
`BITBUCKET_ACCOUNT_SCOPED_TOKEN`) as environment variables, or a `.env`
file dropped next to `datalabs_paths.py`. Not needed for public repos or
local paths.

Output lands in `repo_metrics_docker_package/outputs/repo-metrics/run1/` —
self-contained, same as Docker's bind-mounted output directory.

## Fallback ladder (both run modes — this doesn't change based on Docker vs. Python)

| Situation | Falls back to | Recoverable? |
|---|---|---|
| `scc` missing | *(nothing — hard stop, exit 2)* | No — the one non-recoverable prerequisite |
| `jscpd`/Node missing | built-in duplication detector (`duplication_engine=builtin-fallback`) | Yes, automatic |
| `--build-check docker` but the daemon is unreachable | `--build-check detect` (static identification only) | Yes, automatic |
| Sampling a repo whose metrics clone was partial/blobless (repo_metrics.py's default) | a fresh **full** re-clone from origin, just for the sample step | Yes, automatic — costs a second network fetch; avoid entirely with `--full-clone` |
| `git gc --aggressive` (opt-in) exceeds `--aggressive-timeout` | plain `git gc --prune=now` — same correctness (all unreachable history still dropped), less compression | Yes, automatic |
| Fewer than `--min-commits` (default 20) on the branch being sampled | sampling **skipped** for that repo — not an error; metrics for it still run | N/A by design |
| One repo's metrics or sample step throws an exception | recorded as `error`/`skipped` for that repo only | N/A by design — the batch always continues, one bad repo never aborts the run |

## Output files, in one run

- `repo_metrics.csv` / `.jsonl`, `summary.json`, `artifacts/*.json` — unchanged
  `repo_metrics.py` output.
- `jscpd/<repo>/jscpd-report.json` — jscpd's own raw report per repo, written
  whenever jscpd (not the builtin-fallback detector) is the duplication
  engine; also unchanged `repo_metrics.py` output.
- `samples/<repo>/` — the sliced sample repo, with a `SAMPLE_MANIFEST.json`
  inside recording exactly which commit it was cut at and how many commits
  actually ended up reachable (occasionally a few less than the requested
  index — normal at merge commits, not a bug).
- `sample_report.csv` / `.jsonl` — one row per repo: `ok` / `skipped` / `error`
  plus the reason.
- `run_report.json` — combined totals for both halves, and which setup-time
  fallbacks fired (jscpd, docker-check) for this run.

## What was actually tested before this was packaged

Both `make_sample_repo.py` and `run_metrics_and_sample.py` were run against
real repos before shipping — not just written and assumed correct:

- a full local clone, a partial/blobless local clone (triggers the
  fresh-re-clone fallback), and a bare remote URL
- the `--force`, `--min-commits`, and `--skip-sampling` guards
- the `--aggressive` gc timeout → plain-prune fallback, deliberately forced
  with a 3-second timeout against a large-binary-history repo (the same
  shape of repo that once made an un-bounded `--aggressive` run hang for
  20+ minutes by hand)
- the Docker image, built and run end-to-end from **this exact package
  directory** (not the original development tree), mounting a local repo
  and an output directory, producing byte-identical results to the
  un-containerized run
