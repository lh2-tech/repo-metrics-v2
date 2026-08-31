"""Shared DataLabs workspace paths for repo-metrics scripts.

Thin shim over the workspace-wide `datalabs_paths` module so the three
invariants (one .env, one outputs root, one repo store) come from a single
definition. Import from here; never recompute these paths locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

DATALABS = Path(__file__).resolve().parents[3]
if str(DATALABS) not in sys.path:
    sys.path.insert(0, str(DATALABS))

from datalabs_paths import (  # noqa: E402,F401
    CLONES_ROOT,
    ENV_FILE,
    OUTPUTS_ROOT,
    anthropic_key,
    clones_bare,
    clones_batch,
    clones_github,
    clones_gitlab,
    default_clone_search_roots,
    ensure_clone_dirs,
    ensure_outputs,
    ensure_parent,
    env,
    github_token,
    gitlab_token,
    load_env,
    openai_key,
    outputs_for,
    tool,
    tools_path_entries,
)

# This skill's slice of the one outputs tree: outputs/repo-metrics/...
SKILL = "repo-metrics"


def out(*parts: str) -> Path:
    """outputs/repo-metrics/<parts...> — the only place this skill may write."""
    return outputs_for(SKILL, *parts)


def out_dir(*parts: str) -> Path:
    """Same as out(), with the directory created."""
    return ensure_outputs(SKILL, *parts)


def resolve_out(path) -> Path:
    """Anchor a user-supplied output path to the one outputs root.

    A relative path lands under outputs/repo-metrics/; an absolute path is honoured
    as given, so ad-hoc destinations stay possible.
    """
    p = Path(path)
    return p if p.is_absolute() else out(str(p))


DEFAULT_OUTPUTS = out()
DEFAULT_CLONES = CLONES_ROOT
DEFAULT_SEARCH_ROOTS = default_clone_search_roots()
