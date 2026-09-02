"""Guess a repo's test command and base branch so `bossctl add PATH` needs nothing else."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .util import BossError, git


def _text(path: Path) -> str:
    """Repository metadata that cannot be read is an error to report, not "no tests"."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise BossError(f"cannot read {path} while detecting the test command: {exc}") from None


def test_command(repo: Path) -> str | None:
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            manifest = json.loads(_text(pkg))
        except json.JSONDecodeError as exc:
            raise BossError(f"{pkg} is not valid JSON ({exc}); fix it or pass --test explicitly") from None
        scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
        test_script = (scripts or {}).get("test") if isinstance(scripts, dict) else None
        # A placeholder "no test specified" must not count as a real test gate.
        if test_script and "no test specified" not in test_script:
            for lock, tool in (("pnpm-lock.yaml", "pnpm"),
                               ("yarn.lock", "yarn"),
                               ("bun.lockb", "bun"), ("bun.lock", "bun")):
                if (repo / lock).exists():
                    return f"{tool} test"
            if (repo / "pnpm-workspace.yaml").exists() and (repo / "pnpm-lock.yaml").exists():
                return "pnpm test"
            return "npm test"
    # justfile: a real `test` recipe is enough; absent recipe is a no-op.
    just = repo / "justfile"
    if just.exists() and re.search(r"^test:", _text(just), re.M):
        return "just test"
    if (repo / "Cargo.toml").exists():
        return "cargo test"
    if (repo / "go.mod").exists():
        return "go test ./..."
    if (repo / "MODULE.bazel").exists() or (repo / "WORKSPACE").exists():
        return "bazel test //..."
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "setup.cfg").exists() \
            or list(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        if (repo / "uv.lock").exists():
            return "uv run pytest -q"
        if (repo / "poetry.lock").exists():
            return "poetry run pytest -q"
        if (repo / "mise.toml").exists() and re.search(r"^\[tasks\]\s*\n.*pytest", _text(repo / "mise.toml"), re.M | re.S):
            return "mise run pytest"
        if (repo / ".venv" / "bin" / "python").exists():
            return ".venv/bin/python -m pytest -q"
        return "python3 -m pytest -q"
    if (repo / "Gemfile").exists():
        return "bundle exec rspec" if (repo / "spec").is_dir() else "bundle exec rake test"
    if (repo / "mix.exs").exists():
        return "mix test"
    mk = repo / "Makefile"
    if mk.exists() and re.search(r"^test:", _text(mk), re.M):
        return "make test"
    return None


def base_branch(repo: Path) -> str:
    for ref in ("refs/remotes/origin/HEAD",):
        out = git(repo, "symbolic-ref", "--short", ref, check=False)
        if out:
            return out.split("/", 1)[-1]
    head = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    if head:
        return head
    for cand in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", cand, check=False):
            return cand
    return "main"
