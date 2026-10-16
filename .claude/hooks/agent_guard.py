"""
Claude Code PreToolUse hook: a minimal permission check on every tool call.
Stdlib only, no framework. Wired up in .claude/settings.json.

Protocol (Claude Code hooks): the tool call arrives as JSON on stdin
({"tool_name": ..., "tool_input": {...}, "cwd": ..., ...}). This script then
  - exits 2 with a reason on stderr    -> the call is BLOCKED (reason goes to Claude)
  - prints a JSON "ask" decision, exit 0 -> the user is asked to confirm
  - exits 0 with no output             -> normal permission handling applies

Three checks, in order:
  1. Hard block  - shell commands that are never run (BLOCKED_PATTERNS).
  2. Scope guard - a file WRITE outside the allowed folders asks first.
                   Reads are already limited by settings.json's
                   blockReadsOutsideWorkingDirectories.
  3. Confirm     - deletions and writes to the outside world (Vince Live
                   API, Notion sync, a real app-import) ask first. Plain
                   shell use is NOT on this list: asking before every
                   command would make the agent unusable.

FAILS OPEN on its own bugs: an exception here prints a warning and lets the
call through, because a guard that crashes on every call would block every
tool, including the ones needed to fix the guard.

Audit log: RUN/agent_audit.jsonl (gitignored), one line per call that was
blocked or asked about. Inputs are truncated and credentials redacted - tool
inputs here routinely carry bearer tokens.
"""

import datetime
import json
import os
import re
import sys

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHELL_TOOLS = {"Bash", "PowerShell"}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# 1. Shell commands that are always blocked outright (no override).
#    Checked against shell commands only - a file that merely MENTIONS
#    "DROP TABLE" (like this one) must still be editable.
BLOCKED_PATTERNS = [
    (r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|[A-Za-z]:[\\/]?)(\s|\*|$)", "recursive delete of a root/home directory"),
    (r"\bRemove-Item\b.*-Recurse.*\s([A-Za-z]:\\?|\\|~)(\s|$)", "recursive delete of a drive root/home"),
    (r"\bDROP\s+TABLE\b", "DROP TABLE"),
    (r"\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b)", "force push"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", "fork bomb"),
]

# 2. Folders the agent may WRITE to without asking. Everything else asks.
ALLOWED_WRITE_DIRS = [
    PROJECT_DIR,
    os.path.join(os.path.dirname(PROJECT_DIR), "M3 APIs"),   # sibling working dir
    os.path.join(os.path.expanduser("~"), ".claude"),        # memory, skills
    os.environ.get("TEMP") or os.environ.get("TMP") or "",   # scratchpad lives here
]

# 3. Shell commands that need explicit confirmation.
CONFIRM_PATTERNS = [
    (r"\b(rm|del|rmdir|Remove-Item|erase)\b", "deletes files"),
    (r"api\.vince\.live", "calls the Vince Live API directly"),
    (r"\bcli\.py\s+app-import\b(?!.*--dry-run)", "real (non-dry) app import into a tenant"),
]

AUDIT_LOG_PATH = os.path.join(PROJECT_DIR, "RUN", "agent_audit.jsonl")

# Credentials that show up in tool inputs: Cognito JWTs, Authorization
# headers, Notion integration tokens.
_REDACTIONS = [
    re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),
    re.compile(r"(?i)(bearer\s+)[\w.\-]+"),
    re.compile(r"\b(secret|ntn)_[A-Za-z0-9]{20,}"),
]


def _norm(path: str) -> str:
    # Git Bash style /c/Users/... -> C:/Users/...
    m = re.match(r"^/([a-zA-Z])/(.*)", path)
    if m:
        path = f"{m.group(1)}:/{m.group(2)}"
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, folder: str) -> bool:
    if not folder:
        return False
    path, folder = _norm(path), _norm(folder)
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def check(tool_name: str, tool_input: dict):
    """(decision, reason): decision is 'block', 'ask' or None."""
    if tool_name in SHELL_TOOLS:
        command = str(tool_input.get("command", ""))
        for pattern, why in BLOCKED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return "block", f"agent_guard: blocked ({why}). This command is never run by the agent."
        for pattern, why in CONFIRM_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return "ask", f"agent_guard: this command {why} - confirm it."

    if tool_name in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and not any(_inside(path, d) for d in ALLOWED_WRITE_DIRS):
            return "ask", f"agent_guard: write outside the allowed folders ({path}) - confirm it."

    return None, None


def _redact(text: str) -> str:
    for pattern in _REDACTIONS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "<redacted>", text)
    return text


def _audit(tool_name, tool_input, decision, reason):
    summary = {k: (v if not isinstance(v, str) or len(v) <= 500 else v[:500] + f"... ({len(v)} chars)")
               for k, v in tool_input.items()}
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tool": tool_name,
        "input": json.loads(_redact(json.dumps(summary))),
        "decision": decision,
        "reason": reason,
    }
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass   # logging must never decide whether a call runs


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool_name = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        decision, reason = check(tool_name, tool_input)
    except Exception as error:   # fail open - see the docstring
        print(f"agent_guard: internal error, call NOT checked: {error}", file=sys.stderr)
        return 0

    if decision is None:
        return 0
    _audit(tool_name, tool_input, decision, reason)
    if decision == "block":
        print(reason, file=sys.stderr)
        return 2
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
