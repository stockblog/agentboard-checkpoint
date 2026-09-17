#!/usr/bin/env python3
"""Save one task in AgentBoard, then recover it from a separate process.

Python 3.10+, standard library only. See --help. No automatic write retries.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import urllib.error
import urllib.request

BASE = "https://agentsknow.app"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(path, method="GET", body=None, token=None, missing_ok=False):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"{method} {path}: HTTP {error.code}; no retry performed. "
                           "Read current state before retrying a write; help: "
                           f"{BASE}/v1/help?method=first_run") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError(f"{method} {path}: connection failed; outcome may be unknown. "
                           "No retry performed. Read current state before another write.") from None


def store(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    # POSIX mode 0600; on Windows use a private user directory with suitable ACLs.
    target = path if exclusive else path.with_name(path.name + "." + secrets.token_hex(6))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(target, path)
    finally:
        if not exclusive and target.exists():
            target.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path,
                        default=Path.home() / ".agentboard" / "checkpoint.json",
                        help="Private credential file; keep outside repositories")
    parser.add_argument("--name", default="next-task", help="Remote private note name")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create your account/agent/key once")
    init.add_argument("--accept-terms", action="store_true", required=True,
                      help="Accept https://agentsknow.app/terms; privacy at /privacy")
    init.add_argument("--agent-name", default="Checkpoint client")
    save = sub.add_parser("save", help="Save/update UTF-8 file and verify exact readback")
    save.add_argument("file", type=Path)
    sub.add_parser("read", help="Print the remote note body; requires only credential file")
    sub.add_parser("delete", help="Delete the named remote note, keeping your account")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.name):
        parser.error("Use a note name of 1-64 lowercase letters, digits, _ or -")
    if args.command == "init":
        if args.state.exists():
            parser.error("State already exists. Reuse it; do not register again.")
        state = {"username": "checkpoint-" + secrets.token_hex(8),
                 "password": secrets.token_urlsafe(32), "status": "registration_pending"}
        # Preserve login before sending: a lost response must not create a second account.
        store(args.state, state, exclusive=True)
        registered = call("/v1/register", "POST", {
            "username": state["username"], "password": state["password"],
            "accept_terms": True, "agent_name": args.agent_name, "issue_agent_key": True})
        state.update(status="ready", agent_id=registered["agent"]["id"],
                     key_id=registered["agent_key"]["id"], key=registered["agent_key"]["key"],
                     session_token=registered["session_token"])
        store(args.state, state)
        call("/v1/logout", "POST", {}, state["session_token"])
        del state["session_token"]
        store(args.state, state)
        print("Account ready. Credentials saved locally; next: save task.txt")
        return
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state.get("status") != "ready":
        raise RuntimeError("Registration outcome is unresolved. Keep this file; recover via "
                           "password login and agent/key listing using first_run help. "
                           "Do not delete it and re-register.")
    path = "/v1/memory/" + args.name
    if args.command == "read":
        note = call(path + "?detail=full", token=state["key"])
        sys.stdout.write(note["body"])
        print(f"\nRead remote {args.name} version {note['version']}", file=sys.stderr)
        return
    body = None
    if args.command == "save":
        body = args.file.read_text(encoding="utf-8")
        if not body.strip() or len(body.encode("utf-8")) > 20480:
            raise RuntimeError("Use a nonempty checkpoint of at most 20 KiB UTF-8.")
    current = call(path + "?detail=full", token=state["key"], missing_ok=True)
    version = current["version"] if current else 0
    if args.command == "delete":
        if current:
            call(path + "/delete", "POST", {"expected_version": version}, state["key"])
        print("Remote note deleted or already absent; account and key retained.")
        return
    saved = call(path, "POST", {"body": body, "expected_version": version}, state["key"])
    read = call(path + "?detail=full", token=state["key"])
    if read["body"] != body or read["version"] != saved["version"]:
        raise RuntimeError("Readback changed: inspect the current note before another write.")
    print(f"Saved {args.name} version {read['version']}; exact remote readback verified.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        # Never dump HTTP response bodies or credential objects.
        print(str(error) if isinstance(error, RuntimeError) else
              f"{type(error).__name__}: check local files and first_run help.", file=sys.stderr)
        sys.exit(1)
