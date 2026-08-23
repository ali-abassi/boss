"""Check exact changed path records against protected globs.

Production callers use ``--nul`` with Git's ``-z`` output.  The newline mode
remains only for backwards-compatible direct use; it cannot represent embedded
newlines and is never used by a workflow gate.
"""
import os
import sys
from fnmatch import fnmatch

nul = len(sys.argv) > 1 and sys.argv[1] == "--nul"
patterns = sys.argv[2:] if nul else sys.argv[1:]
if nul:
    paths = [os.fsdecode(raw) for raw in sys.stdin.buffer.read().split(b"\0") if raw]
else:
    paths = [line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]
bad = [path for path in paths if any(fnmatch(path, pattern) for pattern in patterns)]
if bad:
    print("protected paths changed: " + ", ".join(repr(path) for path in bad))
    sys.exit(1)
print("protected paths untouched")
