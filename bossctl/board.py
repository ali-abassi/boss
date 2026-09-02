"""The ops board: what the boss sees at a glance (banner, `bossctl watch`)."""
from __future__ import annotations
import datetime as _dt
import os
import shutil
import time
import unicodedata
from . import registry, work

BANNER = "\n  ◆  B O S S   ·   O P E R A T I O N S\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
PLAIN_BANNER = "\n  [B] BOSS | OPERATIONS\n  -------------------------------------\n"

STATUS_ICON = {"queued": "·", "running": "▶", "paused": "Ⅱ", "needs-you": "?", "ready": "✓", "pr-open": "⇡",
               "failed": "✗", "merged": "⇣", "done": "✓", "cancelled": "–"}
PLAIN_STATUS_ICON = {"queued": ".", "running": ">", "paused": "=", "needs-you": "?", "ready": "+", "pr-open": "+",
                     "failed": "!", "merged": "+", "done": "+", "cancelled": "-"}


def plain() -> bool:
    return os.environ.get("TERM") == "dumb" or os.environ.get("BOSS_PLAIN") == "1"


def no_ansi() -> bool:
    return plain() or os.environ.get("NO_COLOR") is not None


def ascii_text(text: str) -> str:
    """Keep arbitrary repository text useful and unambiguous on ASCII-only transports."""
    punctuation = {"—": "-", "–": "-", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"'}
    out = []
    for char in unicodedata.normalize("NFKD", str(text)):
        if char in punctuation:
            out.append(punctuation[char])
        elif ord(char) < 128:
            out.append(char)
        elif unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
            continue
        elif ord(char) <= 0xFFFF:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(f"\\U{ord(char):08x}")
    return "".join(out)


def cell_width(text: str) -> int:
    """Terminal display cells without assuming one Unicode code point is one cell."""
    width = 0
    for char in text:
        if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def fit_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    used, chars = 0, []
    for char in text:
        cells = cell_width(char)
        if used + cells > width:
            break
        chars.append(char); used += cells
    return "".join(chars)


def pad_width(text: str, width: int) -> str:
    """Fit and right-pad a field to an exact terminal-cell width."""
    fitted = fit_width(text, width)
    return fitted + " " * max(0, width - cell_width(fitted))


def first_line(text) -> str:
    """The item's title line; whitespace-only text must not raise on the board."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return "(untitled)"


def age(stamp: str | None, *, now: _dt.datetime | None = None) -> str:
    """Compact elapsed time since a control-plane timestamp (`2m`, `3h`, `4d`)."""
    if not stamp:
        return "?"
    try:
        then = _dt.datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return "?"
    seconds = int(((now or _dt.datetime.now(_dt.timezone.utc)) - then).total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def header(width: int | None = None) -> str:
    width = width or shutil.get_terminal_size((100, 30)).columns
    return "\n".join(fit_width(line, max(1, width - 1))
                     for line in (PLAIN_BANNER if plain() else BANNER).split("\n"))


MAX_BOARD_ROWS = 12
# A whole unmounted volume must not push live work off the screen.
MAX_WARNING_ROWS = 3


def render(workers, width: int | None = None) -> str:
    """The board. `workers` is the list of positively live worker pids (a bare pid is tolerated)."""
    width = width or shutil.get_terminal_size((100, 30)).columns
    pids = list(workers) if isinstance(workers, (list, tuple)) else ([workers] if workers else [])
    projects = registry.load(check_paths=True)["projects"]
    items = work.all_items()
    open_items = [i for i in items if i["status"] in work.OPEN]
    lines = []
    lines.append(f"  team      {'ready' if pids else 'stopped'}" + (f"   {len(pids)} worker{'s' if len(pids) != 1 else ''}" if pids else ""))
    separator = " | " if plain() else " · "
    lines.append(f"  projects  {len(projects)}" + ("   " + separator.join(f"{p['id']} [{p['mode']}/a{p['authority']}]" for p in list(projects.values())[:6]) if projects else '   none yet - say: "add ~/code/my-repo"'))
    missing = [p for p in projects.values() if not p.get("available", True)]
    for p in missing[:MAX_WARNING_ROWS]:
        where = p.get("path") or "(no path recorded)"
        lines.append(f"  {'!!' if plain() else '⚠'} {p.get('id', '?')}: path missing - {where} is not a Git checkout; new work is refused")
    if len(missing) > MAX_WARNING_ROWS:
        lines.append(f"  ... and {len(missing) - MAX_WARNING_ROWS} more project(s) with a missing path (bossctl projects)")
    needs = [i for i in items if i["status"] in ("needs-you", "failed", "ready", "pr-open")]
    questions = sum(i["status"] == "needs-you" for i in needs)
    ready = sum(i["status"] in ("ready", "pr-open") for i in needs)
    summary = separator.join(x for x in (f"{questions} question{'s' if questions != 1 else ''}" if questions else "",
                                              f"{ready} ready to merge" if ready else "") if x)
    lines.append(f"  inbox     {summary or f'{len(needs)} action(s)'}" if needs else "  inbox     clear")
    from . import supervisor
    supervised = supervisor.summary()
    if supervised.get("away"):
        pending = supervised.get("pending_wakes")
        lines.append(f"  away      on{separator}{pending if pending is not None else '?'} durable decision wake(s) preserved")
    elif supervised.get("pending_wakes"):
        lines.append(f"  wakes     {supervised['pending_wakes']} pending")
    if open_items:
        lines.append("")
        hidden = len(open_items) - MAX_BOARD_ROWS
        if hidden > 0:
            lines.append(f"  ... {hidden} older open item{'s' if hidden != 1 else ''} not shown (bossctl work)")
        for i in open_items[-MAX_BOARD_ROWS:]:
            icon = (PLAIN_STATUS_ICON if plain() else STATUS_ICON).get(i["status"], " ")
            status = ascii_text(i["status"]) if plain() else i["status"]
            project = ascii_text(i["project"]) if plain() else i["project"]
            text = first_line(i["text"])
            extra = (i.get("ask") or {}).get("question") or i.get("pr_url") or ""
            if plain():
                text, extra = ascii_text(text), ascii_text(extra)
            # id, age since last change, and attempt count: a wedged item must not look
            # identical to a healthy one, and every row must be addressable by name.
            attempts = f"a{i.get('attempts', 0)}/{i.get('max_attempts', '?')}"
            meta = f"{i.get('id', '?')} {age(i.get('updated') or i.get('created'))} {attempts}"
            row = f"  {icon} {pad_width(status, 9)} {pad_width(project, 12)} {text}  [{meta}]"
            if extra:
                row += f"  {'-' if plain() else '—'} {extra}"
            lines.append(row)
    if plain():
        lines = [ascii_text(line) for line in lines]
    return "\n".join(fit_width(line, max(1, width - 1)) for line in lines)


def watch(interval: float, once: bool = False) -> None:
    from .cli import daemon_pids
    while True:
        width = shutil.get_terminal_size((100, 30)).columns
        separator = " | " if plain() else " · "
        tail = "" if once else f"\n\n  {time.strftime('%H:%M:%S')}{separator}refreshing every {interval:g}s{separator}ctrl-c to stop\n"
        fitted_tail = "\n".join(fit_width(line, max(1, width - 1)) for line in tail.split("\n"))
        out = header(width) + render(daemon_pids(), width) + fitted_tail
        if not once and not no_ansi():
            print("\033[2J\033[H", end="")
        print(out)
        if once:
            return
        time.sleep(interval)
