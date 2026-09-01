# repo-metrics-sample, containerized

One container, one run: due-diligence metrics **and** a first-N% commit-history
sample, for every repo in a list. Runs `run_metrics_and_sample.py`, which
combines `repo_metrics.py` and `make_sample_repo.py` by composition (neither
is reimplemented for the container).

## Build

Build context must be **this repo's root**, not this `docker/` directory —
the Dockerfile needs `datalabs_paths.py` and `skills/repo-metrics/scripts/`:

```bash
cd /path/to/this-repo
docker build -f skills/repo-metrics/docker/Dockerfile -t repo-metrics-sample .
```

## Quickest: docker compose

`docker-compose.yml` at this repo's root wires up the three bind mounts
below for you, so `build` + `run` collapse to:

```bash
cd /path/to/this-repo
cp .env.example .env    # regular runs: leave it as-is; see main README's Tokens section for private repos
docker compose build
docker compose run --rm repo-metrics --repos /data/repos.txt --out run1 --pct 5
```

Args after `repo-metrics` pass straight through to
`run_metrics_and_sample.py`. The optional variants below (local-repo mount,
clone-cache persistence, docker-socket mount) are commented-out lines in
`docker-compose.yml` — uncomment the one you need, or add it ad hoc with
`docker compose run --rm -v host:container repo-metrics ...`.

The rest of this file documents the equivalent manual `docker build`/`docker
run` commands and every mount in full — useful if you're not using compose,
or want to see exactly what's crossing the container boundary.

## Run (manual `docker run`)

Three things need to cross the container boundary: your repo list, your
tokens, and the output directory.

```bash
docker run --rm \
  -v "$PWD/repos.txt:/data/repos.txt:ro" \
  -v "$PWD/run-output:/app/outputs/repo-metrics" \
  -v "$PWD/.env:/app/.env:ro" \
  repo-metrics-sample \
  --repos /data/repos.txt --out run1 --pct 5
```

- `repos.txt` — same format `repo_metrics.py` always accepted: one entry per
  line (`owner/repo`, a full URL, or a local path — see below for how local
  paths work inside a container).
- `run-output` — bind-mount this or every result disappears with the
  container. Everything (`repo_metrics.csv`, `samples/`, `run_report.json`,
  ...) lands under `run-output/run1/`.
- `.env` — `GH_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN` /
  `BITBUCKET_ACCOUNT_SCOPED_TOKEN`, one `KEY=value` per line. Not needed for
  public repos. Env vars work too: `-e GH_TOKEN=...`.

### Sampling a local repo already on your machine

Mount it in and point `repos.txt` at the path *inside the container*:

```bash
docker run --rm \
  -v "$PWD/repos.txt:/data/repos.txt:ro" \
  -v "$PWD/run-output:/app/outputs/repo-metrics" \
  -v "/path/to/some-repo:/data/some-repo:ro" \
  repo-metrics-sample \
  --repos /data/repos.txt --out run1
```

where `repos.txt` contains the line `/data/some-repo`.

### Persisting the clone cache across runs (optional)

Without this, every run clones fresh. With it, repeat runs reuse what's
already there:

```bash
  -v "$PWD/clones-cache:/app/clones" \
```

### `--build-check docker` (real build/test attempts, not static detection)

Needs the host's own docker socket, since this image does not run a nested
daemon:

```bash
  -v /var/run/docker.sock:/var/run/docker.sock \
```

Without that mount, the daemon is unreachable from inside the container and
`repo_metrics.py` **automatically downgrades to `--build-check detect`** —
this is the existing, unchanged fallback, not something the container adds.

## Fallback ladder (identical to running the scripts un-containerized)

| Missing / failing | Falls back to | Recoverable? |
|---|---|---|
| `scc` binary | *(nothing — hard stop, exit 2)* | No — rebuild the image; this is the one thing with zero fallback |
| `jscpd` / Node | built-in duplication detector (rows read `duplication_engine=builtin-fallback`) | Yes, automatic — this image installs jscpd, so it should not fire unless the npm install step failed at build time |
| `docker` daemon unreachable (`--build-check docker`) | `--build-check detect` (static identification only) | Yes, automatic — mount the socket to avoid it |
| A repo's clone is partial/blobless (default for remote repos) | sampling re-clones that one repo fresh and full from origin | Yes, automatic — costs a second network fetch per repo; pass `--full-clone` to avoid it entirely |
| `git gc --aggressive` exceeds `--aggressive-timeout` | plain `git gc --prune=now` (same correctness, less compression) | Yes, automatic — only relevant if you pass `--aggressive` at all (off by default) |
| Fewer than `--min-commits` (default 20) on the sampled branch | sampling is **skipped** for that repo, not treated as an error | N/A by design — metrics for that repo still run normally |
| One repo's metrics OR sample step throws | recorded as `error`/`skipped` for that repo only | N/A by design — the batch always continues |

See `run_metrics_and_sample.py`'s own module docstring for the same table
with more detail; this container changes none of that logic.

## What's in the image vs. what crosses the boundary

Baked in: Python 3.12, `git`, `scc` (pinned version, arch-matched at build
time via `TARGETARCH`), Node + `jscpd`, the `docker` CLI binary (not a
daemon).

Never baked in: tokens, repo lists, output data, the clone cache. All four
cross via bind mounts or env vars at `docker run` time, per above.
