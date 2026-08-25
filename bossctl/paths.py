from __future__ import annotations
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GRAPHS = REPO / "graphs"


def home() -> Path:
    return Path(os.environ.get("BOSS_HOME", "~/.boss")).expanduser().resolve()


def projects_file() -> Path: return home() / "projects.json"
def dispatch_file() -> Path: return home() / "dispatch.json"
def work_root() -> Path: return home() / "work"
def worktree_root() -> Path: return home() / "worktrees"
def log_file() -> Path: return home() / "bossctl.log"
def supervisor_file() -> Path: return home() / "supervisor.json"
def wakes_file() -> Path: return home() / "wakes.json"
def planning_file() -> Path: return home() / "planning.json"
def supervisor_lock() -> Path: return home() / "supervisor.lock"
def planning_lock() -> Path: return home() / "planning.lock"
def authority_lock() -> Path: return home() / "authority.lock"
