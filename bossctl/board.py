"""The ops board: what the boss sees at a glance (banner, `bossctl watch`)."""
from __future__ import annotations
import os
import shutil
import time
import unicodedata
from . import registry, work
from .paths import home

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


def header(width: int | None = None) -> str:
    width = width or shutil.get_terminal_size((100, 30)).columns
    return "\n".join(fit_width(line, max(1, width - 1))
                     for line in (PLAIN_BANNER if plain() else BANNER).split("\n"))


def render(workers_pid: int | None, width: int | None = None) -> str:
    width = width or shutil.get_terminal_size((100, 30)).columns
    projects = registry.load()["projects"]
    items = work.all_items()
    open_items = [i for i in items if i["status"] in work.OPEN]
    lines = []
    lines.append(f"  team      {'ready' if workers_pid else 'stopped'}")
    separator = " | " if plain() else " · "
    lines.append(f"  projects  {len(projects)}" + ("   " + separator.join(f"{p['id']} [{p['mode']}/a{p['authority']}]" for p in list(projects.values())[:6]) if projects else '   none yet - say: "add ~/code/my-repo"'))
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
        for i in open_items[-12:]:
            icon = (PLAIN_STATUS_ICON if plain() else STATUS_ICON).get(i["status"], " ")
            status = ascii_text(i["status"]) if plain() else i["status"]
            project = ascii_text(i["project"]) if plain() else i["project"]
            text = i["text"].splitlines()[0]
            extra = (i.get("ask") or {}).get("question") or i.get("pr_url") or ""
            if plain():
                text, extra = ascii_text(text), ascii_text(extra)
            row = f"  {icon} {pad_width(status, 9)} {pad_width(project, 12)} {text}"
            if extra:
                row += f"  {'-' if plain() else '—'} {extra}"
            lines.append(row)
    if plain():
        lines = [ascii_text(line) for line in lines]
    return "\n".join(fit_width(line, max(1, width - 1)) for line in lines)


def banner(workers_pid: int | None) -> str:
    width = shutil.get_terminal_size((100, 30)).columns
    home_line = "  home " + str(home())
    if plain():
        home_line = ascii_text(home_line)
    return header(width) + render(workers_pid, width) + "\n\n" + fit_width(home_line, width - 1) + "\n"


def watch(interval: float, once: bool = False) -> None:
    from .cli import daemon_pid
    while True:
        width = shutil.get_terminal_size((100, 30)).columns
        separator = " | " if plain() else " · "
        tail = "" if once else f"\n\n  {time.strftime('%H:%M:%S')}{separator}refreshing every {interval:g}s{separator}ctrl-c to stop\n"
        fitted_tail = "\n".join(fit_width(line, max(1, width - 1)) for line in tail.split("\n"))
        out = header(width) + render(daemon_pid(), width) + fitted_tail
        if not once and not no_ansi():
            print("\033[2J\033[H", end="")
        print(out)
        if once:
            return
        time.sleep(interval)
