"""Run Claude Code as a headless, sandboxed first responder for one incident.

Safety comes from stacking two independent mechanisms, not one:
  1. `--restricted` starts the session with NO code-running tools at all and
     ignores this machine's own user/project settings files, then `--tools`
     explicitly re-adds only what we name.
  2. `--allowedTools`/`--disallowedTools` narrows those tools further (which
     paths Edit may touch, which exact Bash commands are allowed).
`--permission-mode dontAsk` + `--permission-prompts none` mean anything not
covered by the above is denied outright -- there is no human to ask in
headless mode, so "ask" would just hang forever.
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

AGENT_BIN = os.getenv("RESPONDER_AGENT_BIN", "claude")
AGENT_TIMEOUT_SECONDS = int(os.getenv("RESPONDER_AGENT_TIMEOUT", "900"))
AGENT_MAX_BUDGET_USD = os.getenv("RESPONDER_MAX_BUDGET_USD", "2")

ALLOWED_TOOLS = [
    "Edit(./app/**)",
    "Edit(./tests/**)",
    "Bash(uv run --frozen pytest:*)",
    "Bash(incident-response/runbooks/redeploy.sh)",
    "Bash(incident-response/runbooks/verify-recovery.sh:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
]
DISALLOWED_TOOLS = [
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git reset:*)",
    "Bash(git checkout:*)",
    "Bash(docker:*)",
]

# A child `claude` must not think it is running inside this session's own
# agent loop (this responder itself runs on a machine where Claude Code is
# already installed and logged in).
_SESSION_VAR = re.compile(r"^CLAUDE(CODE$|_CODE_|_PID$|_EFFORT$)")
_KEEP_VARS = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}


def _agent_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in _KEEP_VARS or not _SESSION_VAR.match(k)}


def _command() -> list[str]:
    return [
        AGENT_BIN,
        "-p",
        "--restricted",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--max-budget-usd", AGENT_MAX_BUDGET_USD,
        "--tools", "Read,Grep,Glob,Edit,Bash",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
    ]


def run(prompt: str, repo_root: Path, transcript_path: Path) -> dict:
    """Run the agent to completion, streaming stream-json events to transcript_path.

    Returns exit code, timing, model/tool metadata, cost, and the parsed answer.
    The prompt is sent over stdin -- passing it as an argv item risks being
    swallowed by the preceding variadic --tools/--allowedTools flags.
    """
    command = _command()
    started = time.monotonic()
    init_event, result_event = {}, {}

    with transcript_path.open("w") as transcript:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=_agent_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        timer = threading.Timer(AGENT_TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                transcript.write(line)
                transcript.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    init_event = event
                elif event.get("type") == "result":
                    result_event = event
            exit_code = process.wait()
        finally:
            timer.cancel()

    answer_text = (result_event.get("result") or "").strip()
    answer_lines = [line for line in answer_text.splitlines() if line.strip()]

    return {
        "command": command,
        "exit_code": exit_code,
        "timed_out": time.monotonic() - started >= AGENT_TIMEOUT_SECONDS,
        "duration_s": round(time.monotonic() - started, 1),
        "model": init_event.get("model"),
        "permission_mode": init_event.get("permissionMode"),
        "tools": init_event.get("tools"),
        "num_turns": result_event.get("num_turns"),
        "cost_usd": result_event.get("total_cost_usd"),
        "is_error": result_event.get("is_error"),
        "permission_denials": result_event.get("permission_denials", []),
        "answer_text": answer_text,
        "last_line": answer_lines[-1] if answer_lines else "",
    }
