"""Explicit narrow memory: local operations ledger or reviewed project change."""
from __future__ import annotations
import json
import re
from pathlib import Path
from . import control, worktree
from .paths import home
from .util import BossError, locked, now, write_json

VERSION = 1
MAX_VALUE = 2000
MAX_ENTRIES = 1000
KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
FORBIDDEN_KEYS = {"transcript", "conversation", "chat-log", "session-log", "session-jsonl"}


def _path() -> Path: return home() / "memory.json"
def _lock() -> Path: return home() / "memory.lock"


def _read() -> dict:
    try: value = json.loads(_path().read_text())
    except FileNotFoundError: return {"version": VERSION, "entries": []}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BossError(f"memory ledger is unreadable: {type(exc).__name__}; run `pi-boss doctor`")
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or not isinstance(value.get("entries"), list) or len(value["entries"]) > MAX_ENTRIES):
        raise BossError("memory ledger schema is unsupported or malformed")
    seen = set()
    for entry in value["entries"]:
        key = entry.get("key") if isinstance(entry, dict) else None
        if (not isinstance(entry, dict) or entry.get("scope") != "operational"
                or entry.get("id") != f"operational:{key}"
                or not isinstance(key, str) or not KEY_RE.fullmatch(key)
                or key in seen or not isinstance(entry.get("value"), str)
                or not isinstance(entry.get("revision"), int) or isinstance(entry.get("revision"), bool)
                or entry.get("revision", 0) < 1
                or not isinstance(entry.get("created"), str) or not isinstance(entry.get("updated"), str)
                or entry.get("source") != "boss-explicit-cli"):
            raise BossError("memory ledger contains a malformed, duplicated, or untrusted entry")
        seen.add(key)
    return value


def _key(value: str) -> str:
    key = value.strip().lower()
    if not KEY_RE.fullmatch(key):
        raise BossError("memory key must be 1-80 lowercase letters, digits, dots, dashes, or underscores")
    if key in FORBIDDEN_KEYS or any(word in key for word in ("transcript", "conversation", "session-jsonl")):
        raise BossError("transcripts and conversation/session dumps are not memory entries")
    return key


def _value(value: str) -> str:
    text = value.strip()
    if not text: raise BossError("memory value cannot be empty")
    if len(text) > MAX_VALUE:
        raise BossError(f"memory value exceeds {MAX_VALUE} characters; store a narrow fact, not a transcript")
    if control.redact(text) != text:
        raise BossError("memory value appears to contain a credential or secret")
    if _looks_like_transcript(text):
        raise BossError("memory value looks like a chat/session transcript; store one narrow fact instead")
    return text


def _looks_like_transcript(text: str) -> bool:
    role_lines = re.findall(r"(?im)^\s*(?:user|assistant|system|developer|tool|human|ai)\s*:\s+", text)
    if len(role_lines) >= 2:
        return True
    if re.search(r'(?i)"(?:role|messages|conversation|transcript|session_id)"\s*:', text):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return True
    if len(text.splitlines()) >= 8 and re.search(r"(?m)^\s*\{\s*\"(?:type|role|message)\"", text):
        return True
    return False


def validate_project_change(item: dict, project: dict, wt: Path) -> str | None:
    """Mechanical last gate for explicit project-memory requests."""
    request = item.get("memory_request")
    if not isinstance(request, dict): return None
    from .util import git
    changed = worktree.changed_files(project, wt)
    if changed != ["AGENTS.md"]:
        return "project memory must change AGENTS.md and no other file"
    path = wt / "AGENTS.md"
    try: content = path.read_text()
    except (OSError, UnicodeError): return "project memory AGENTS.md is unreadable"
    if len(content) > 200_000: return "project memory file is unexpectedly large"
    key, operation = request.get("key"), request.get("operation")
    if operation == "set":
        value = request.get("value")
        if not isinstance(key, str) or not isinstance(value, str): return "project memory request is malformed"
        if content.count(f"`{key}`") != 1 or content.count(value) != 1:
            return "project memory key/value must appear exactly once"
    elif operation == "remove":
        if not isinstance(key, str) or f"`{key}`" in content:
            return "project memory removal did not remove the exact keyed entry"
    else:
        return "project memory operation is malformed"
    added = "\n".join(line[1:] for line in git(wt, "diff", "--no-ext-diff", "--no-textconv",
                                               f"{project['base']}...HEAD", "--", "AGENTS.md").splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    if control.redact(added) != added or _looks_like_transcript(added):
        return "project memory addition contains secret-like or transcript-shaped content"
    return None


def list_operational() -> list[dict]:
    return [dict(entry) for entry in _read()["entries"]]


def get_operational(key: str) -> dict:
    key = _key(key)
    entry = next((entry for entry in _read()["entries"] if entry.get("key") == key), None)
    if not entry: raise BossError(f"unknown operational memory key '{key}'")
    return dict(entry)


def set_operational(key: str, value: str) -> dict:
    key, value = _key(key), _value(value)
    with locked(_lock()):
        data = _read(); entry = next((e for e in data["entries"] if e.get("key") == key), None)
        if entry and entry.get("value") == value:
            return {**entry, "deduplicated": True, "changed": False}
        timestamp = now()
        if entry:
            entry.update(value=value, updated=timestamp, revision=int(entry.get("revision", 1)) + 1)
        else:
            if len(data["entries"]) >= MAX_ENTRIES:
                raise BossError(f"operational memory is at its {MAX_ENTRIES}-entry bound; remove an entry first")
            entry = {"id": f"operational:{key}", "scope": "operational", "key": key, "value": value,
                     "created": timestamp, "updated": timestamp, "revision": 1,
                     "source": "boss-explicit-cli"}
            data["entries"].append(entry)
        write_json(_path(), data)
        return {**entry, "deduplicated": False, "changed": True}


def remove_operational(key: str, *, confirm: bool) -> dict:
    key = _key(key)
    if not confirm:
        raise BossError("removing operational memory requires --confirm")
    with locked(_lock()):
        data = _read(); before = len(data["entries"])
        data["entries"] = [entry for entry in data["entries"] if entry.get("key") != key]
        if len(data["entries"]) == before: raise BossError(f"unknown operational memory key '{key}'")
        write_json(_path(), data)
    return {"scope": "operational", "key": key, "removed": True}


def project_requests(project_id: str) -> dict:
    from . import registry, work
    project = registry.get(project_id)
    requests = []
    for item in work.all_items():
        request = item.get("memory_request")
        if item.get("project") == project_id and isinstance(request, dict):
            requests.append({"item_id": item["id"], "status": item.get("status"), "head_sha": item.get("head_sha"),
                             **request, "created": item.get("created"), "updated": item.get("updated")})
    return {"scope": "project", "project": project_id, "canonical_file": str(Path(project["path"]) / "AGENTS.md"),
            "requests": requests}


def request_project(project_id: str, operation: str, key: str, value: str | None = None, *, confirm: bool = False) -> dict:
    from . import work
    if operation not in ("set", "remove"): raise BossError("project memory operation must be set or remove")
    key = _key(key)
    if operation == "set": value = _value(value or "")
    elif not confirm: raise BossError("removing project memory requires --confirm")
    desired = {"scope": "project", "project": project_id, "operation": operation, "key": key,
               "value": value, "canonical_file": "AGENTS.md", "source": "boss-explicit-cli"}
    ledger = project_requests(project_id)["requests"]
    active = [r for r in ledger if r.get("key") == key and r.get("status") in (*work.OPEN, "failed")]
    if active:
        latest = active[-1]
        if latest.get("operation") == operation and latest.get("value") == value:
            return {"deduplicated": True, "item": work.load(latest["item_id"]), "request": desired}
        raise BossError(f"project memory key '{key}' already has unresolved item {latest['item_id']}; resolve it before updating")
    completed = [r for r in ledger if r.get("key") == key and r.get("status") == "merged"]
    if completed and completed[-1].get("operation") == operation and completed[-1].get("value") == value:
        return {"deduplicated": True, "item": work.load(completed[-1]["item_id"]), "request": desired}
    if operation == "set":
        task = ("Record one narrow piece of durable project knowledge. Update AGENTS.md only. "
                "Under a concise 'Project memory' section, deduplicate by key and set "
                f"`{key}` to this guidance: {value}\n\nDo not copy chat, transcripts, credentials, or unrelated context.")
    else:
        task = ("Remove one piece of durable project knowledge. Update AGENTS.md only. "
                f"Remove the Project memory entry keyed `{key}` without changing unrelated guidance. "
                "Do not copy chat, transcripts, credentials, or unrelated context.")
    item = work.create(project_id, task, labels=["project-memory", "high-risk"], declared_scope=["AGENTS.md"],
                       memory_request=desired)
    return {"deduplicated": False, "item": item, "request": desired}
