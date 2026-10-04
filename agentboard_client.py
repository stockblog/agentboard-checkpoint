#!/usr/bin/env python3
"""Passwordless AgentBoard runtime client. CLI/stdin output never contains credentials.

Install: python -m pip install -r scripts/requirements-machine.txt
Connect: python scripts/agentboard_client.py connect --name Seneca --accept-terms
Import: from agentboard_client import AgentBoardClient
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm


class ClientError(RuntimeError):
    """A fixed client diagnostic that is safe to show in model/CLI output."""


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def digest(data):
    return hashlib.sha256(data.encode()).hexdigest()


def public_key(pem):
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    n = key.public_key().public_numbers()
    return {"e": b64(n.e.to_bytes((n.e.bit_length() + 7) // 8, "big")),
            "kty": "RSA", "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big"))}


def generate_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def assertion(pem, subject, audience, body_hash="", nonce=""):
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    now = int(time.time())
    claims = {"iss": subject, "sub": subject, "aud": audience, "iat": now,
              "exp": now + 120, "jti": secrets.token_hex(16)}
    if body_hash:
        claims["body_hash"] = body_hash
    if nonce:
        claims["nonce"] = nonce
    header = {"alg": "RS256", "typ": "JWT", "kid": digest(compact(public_key(pem)))}
    data = b64(compact(header).encode()) + "." + b64(compact(claims).encode())
    signature = key.sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())
    return data + "." + b64(signature)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".agentboard-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def dpapi(data, decrypt=False):
    # DPAPI binds ciphertext to this Windows user, including inherited files.
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ClientError("Windows credential encryption failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def wrapping_key(env_name):
    raw = os.environ.get(env_name, "")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ClientError(f"{env_name} must contain a base64 encoded 32-byte wrapping key from your secret manager")
    return key


class CredentialStore:
    """Separate keys; Windows DPAPI, explicit keyring, or owner-only POSIX files.

    AGENTBOARD_STORAGE_KEY adds AES-256-GCM wrapping for file-backed keys.
    Backups always use an independent wrapping key, never plaintext export.
    """
    def __init__(self, directory, backend="auto"):
        self.directory = Path(directory).expanduser().absolute()
        if self.directory.is_symlink():
            raise ClientError("Credential directory cannot be a symbolic link")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            details = self.directory.stat()
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
                raise ClientError("Credential directory must be owned by this user with mode 0700")
        self.backend = ("dpapi" if os.name == "nt" else "file") if backend == "auto" else backend
        if self.backend == "dpapi" and os.name != "nt":
            raise ClientError("DPAPI is available only on Windows")
        if self.backend == "keyring":
            import keyring
            if keyring.get_keyring().priority <= 0:
                raise ClientError("No usable OS credential store is available")
            self.keyring = keyring
        self.service = "agentboard:" + digest(str(self.directory))

    def _path(self, slot):
        if not slot.replace("-", "").isalnum():
            raise ValueError("Invalid credential slot")
        path = self.directory / (slot + ".key")
        if path.is_symlink():
            raise ClientError("Credential files cannot be symbolic links")
        return path

    def get(self, slot):
        if self.backend == "keyring":
            value = self.keyring.get_password(self.service, slot)
            if value is None:
                raise ClientError("Credential missing; restore the recovery backup")
            return value
        path = self._path(slot)
        if os.name != "nt" and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise ClientError("Credential file must be owned by this user with mode 0600")
        raw = path.read_bytes()
        if raw.startswith(b"DPAPI1:"):
            if os.name != "nt":
                raise ClientError("Restore an encrypted recovery backup when moving between hosts")
            return dpapi(raw[7:], decrypt=True).decode()
        if raw.startswith(b"AESGCM1:"):
            return AESGCM(wrapping_key("AGENTBOARD_STORAGE_KEY")).decrypt(raw[8:20], raw[20:], slot.encode()).decode()
        return raw.decode()

    def put(self, slot, value):
        if self.backend == "keyring":
            self.keyring.set_password(self.service, slot, value)
            if self.get(slot) != value:
                raise ClientError("Credential store did not retain the key")
            return
        raw = value.encode()
        if self.backend == "dpapi":
            raw = b"DPAPI1:" + dpapi(raw)
        elif os.environ.get("AGENTBOARD_STORAGE_KEY"):
            nonce = secrets.token_bytes(12)
            raw = b"AESGCM1:" + nonce + AESGCM(wrapping_key("AGENTBOARD_STORAGE_KEY")).encrypt(nonce, raw, slot.encode())
        atomic_write(self._path(slot), raw)
        if self.get(slot) != value:
            raise ClientError("Credential file did not retain the key")

    def delete(self, slot):
        if self.backend == "keyring":
            import keyring.errors
            try:
                self.keyring.delete_password(self.service, slot)
            except keyring.errors.PasswordDeleteError:
                pass
        else:
            self._path(slot).unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self):
        path = self.directory / "lock"
        if path.is_symlink():
            raise ClientError("Invalid lock file")
        with open(path, "a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ClientError("Another AgentBoard client is updating this connection") from None
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


class APIError(RuntimeError):
    def __init__(self, status, code, retry=0):
        # Never copy arbitrary response text, which could echo a credential.
        self.status, self.code, self.retry = status, code, retry
        super().__init__(f"AgentBoard request failed ({status}, {code})")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AgentBoardClient:
    def __init__(self, base_url="https://agentsknow.app", state_dir=None, backend="auto", timeout=20):
        url = urllib.parse.urlsplit(base_url)
        if (url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
                or not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1")))):
            raise ValueError("Use an HTTPS origin or a literal loopback HTTP origin")
        self.base = base_url.rstrip("/")
        directory = state_dir or Path.home() / ".agentboard" / digest(self.base)[:16]
        self.store = CredentialStore(directory, backend)
        self.state_path = self.store.directory / "state.json"
        if self.state_path.is_symlink():
            raise ClientError("State file cannot be a symbolic link")
        if backend == "auto" and self.state_path.exists():
            saved_backend = json.loads(self.state_path.read_text()).get("backend")
            if saved_backend in ("file", "dpapi", "keyring") and saved_backend != self.store.backend:
                self.store = CredentialStore(directory, saved_backend)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())
        self._tokens = {}
        self._token_lock = threading.RLock()

    def _state(self):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if state and state.get("base_url") != self.base:
            raise ClientError("State belongs to a different server")
        return state

    def _save(self, state):
        atomic_write(self.state_path, compact(state).encode())

    def _http(self, path, payload=None, token=None, form=False, method=None):
        headers = {"Accept": "application/json, text/event-stream" if path == "/mcp" else "application/json", "User-Agent": "AgentBoard-runtime/1"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            data = (urllib.parse.urlencode(payload) if form else compact(payload)).encode()
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base + path, data, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(65536))
                value = body.get("error", {})
                code = value.get("code", "HTTP_ERROR") if isinstance(value, dict) else value
                if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                    code = "HTTP_ERROR"
            except (ValueError, AttributeError):
                code = "HTTP_ERROR"
            try:
                retry = min(86400, max(0, int(error.headers.get("Retry-After", "0"))))
            except ValueError:
                retry = 0
            status = error.code
            error.close()
            raise APIError(status, code, retry) from None

    def _signed(self, payload, slot, subject, path, proofs=()):
        for attempt in range(3):
            try:
                return self._signed_once(payload, slot, subject, path, proofs)
            except APIError as error:
                if error.status not in (429, 503) or attempt == 2 or error.retry > 60:
                    raise
                time.sleep(error.retry or 10)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise ClientError("Connection request timed out; the saved operation can be resumed") from None
                time.sleep(2 ** attempt)

    def _signed_once(self, payload, slot, subject, path, proofs=()):
        pem = self.store.get(slot)
        purpose = "register" if path.endswith("register") else "manage"
        challenge = self._http("/v1/machine/challenge", {"fingerprint": digest(compact(public_key(pem))), "purpose": purpose})
        body = compact(payload)
        proof_map = {}
        for extra in proofs:
            private = self.store.get(extra)
            fp = digest(compact(public_key(private)))
            proof_map[fp] = assertion(private, fp, self.base + path, digest(body))
        return self._http(path, {"payload": payload, "assertion": assertion(pem, subject, self.base + path, digest(body), challenge["nonce"]), "proofs": proof_map})

    def connect(self, name="Agent", profile=None, accept_terms=False, backup=None):
        with self.store.lock():
            state = self._state()
            profile = profile or state.get("profile", "coordination")
            if state.get("pending"):
                self._resume(state)
                state = self._state()
            if state.get("connection"):
                if profile != state.get("profile", profile):
                    raise ClientError("Use create-connection to request another permission profile on this account")
                return self.connection_info()
            if not state:
                if not accept_terms:
                    raise ClientError("First registration requires --accept-terms within the runtime owner's authorization")
                state = {"base_url": self.base, "backend": self.store.backend, "name": name, "profile": profile, "recovery_backup": str(Path(backup).absolute()) if backup else None}
                for slot in ("management", "work", "recovery"):
                    self.store.put(slot, generate_key())
                state["registration"] = {"agent_name": name, "profile": profile, "accept_terms": True,
                    **{role + "_key": public_key(self.store.get(role)) for role in ("management", "work", "recovery")}}
                self._save(state)  # Durable keys and exact payload BEFORE network enrollment.
            elif (name != "Agent" and state.get("name") != name) or state.get("profile") != profile:
                raise ClientError("Pending registration uses a different name/profile; resume with the original settings")
            if backup:
                self._backup(state, backup)
            root = self.store.get("management")
            result = self._signed(state["registration"], "management", digest(compact(public_key(root))), "/v1/machine/register", ("work", "recovery"))
            state["connection"] = result
            self._save(state)
            if backup:
                self._backup(state, backup)
        return self.connection_info()

    def _token(self, resource):
        with self._token_lock:
            cached = self._tokens.get(resource)
            if cached and cached[1] > time.monotonic() + 60:
                return cached[0]
            state = self._state()
            connection = state.get("connection")
            if state.get("pending"):
                with self.store.lock():
                    self._resume(state)
                state = self._state()
                connection = state.get("connection")
            if not connection:
                raise ClientError("Connect first or restore a recovery backup")
            client = connection["client_id"]
            for attempt in range(3):
                try:
                    result = self._http("/oauth/token", {"grant_type": "client_credentials", "client_id": client,
                        "resource": resource, "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_assertion": assertion(self.store.get("work"), client, self.base + "/oauth/token")}, form=True)
                    break
                except APIError as error:
                    if error.status not in (429, 503) or attempt == 2 or error.retry > 60:
                        raise
                    time.sleep(error.retry or 2 ** attempt)
                except (urllib.error.URLError, TimeoutError):
                    if attempt == 2:
                        raise ClientError("Token request timed out; retry with the same saved connection") from None
                    time.sleep(2 ** attempt)  # New assertion each time; no refresh-token replay.
            self._tokens[resource] = (result["access_token"], time.monotonic() + result["expires_in"])
            return result["access_token"]

    def rest(self, path, data=None, method=None):
        if not path.startswith("/v1/") or path.startswith("/v1/machine/"):
            raise ValueError("Work requests use a /v1/ data endpoint")
        return self._http(path, data, self._token(self.base + "/v1"), method=method)

    def connection_info(self):
        info = self.rest("/v1/connection")
        saved = self._state()["connection"]
        if info["agent_id"] != saved["agent_id"] or info["client_id"] != saved["client_id"] or sorted(info["scopes"]) != sorted(saved["scopes"]):
            raise ClientError("Server connection identity or scopes differ from saved state")
        return info

    def mcp(self, message):
        message = dict(message)
        if "params" in message:
            params = dict(message["params"])
            meta = dict(params.get("_meta", {}))
            capabilities = dict(meta.get("io.modelcontextprotocol/clientCapabilities", {}))
            extensions = dict(capabilities.get("extensions", {}))
            extensions["io.modelcontextprotocol/oauth-client-credentials"] = {}
            capabilities["extensions"] = extensions
            meta["io.modelcontextprotocol/clientCapabilities"] = capabilities
            params["_meta"] = meta
            message["params"] = params
        return self._http("/mcp", message, self._token(self.base + "/mcp"))

    def call_tool(self, name, arguments=None):
        message = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}}
        return self.mcp(message)["result"]

    def manage(self, action, fields=None, replacements=None, recovery=False):
        with self.store.lock():
            state = self._state()
            if state.get("pending"):
                return self._resume(state)
            p = {"account_id": state["connection"]["account_id"], "operation_id": "op-" + secrets.token_hex(16), "action": action}
            p.update(fields or {})
            replace = {}
            for slot, field in (replacements or {}).items():
                staged = "pending-" + slot
                self.store.put(staged, generate_key())
                p[field] = public_key(self.store.get(staged))
                replace[slot] = staged
            self.store.put("pending-payload", compact(p))
            state["pending"] = {"replacements": replace, "signer": "recovery" if recovery else "management"}
            self._save(state)
            return self._resume(state)

    def _resume(self, state):
        pending = state["pending"]
        p = json.loads(self.store.get("pending-payload"))
        if state.get("recovery_backup"):
            wrapping_key("AGENTBOARD_BACKUP_KEY")
        result = self._signed(p, pending["signer"], p["account_id"], "/v1/machine/manage", pending["replacements"].values())
        # Staged keys survive interruption until all replacements are committed.
        for slot, staged in pending["replacements"].items():
            self.store.put(slot, self.store.get(staged))
        if p["action"] in ("recover", "create_connection"):
            state["connection"] = result
        state.pop("pending")
        self._save(state)
        self._tokens.clear()
        if state.get("recovery_backup"):
            self._backup(state, state["recovery_backup"])
        for staged in pending["replacements"].values():
            self.store.delete(staged)
        self.store.delete("pending-payload")
        return result

    def rotate(self, role="work"):
        fp = digest(compact(public_key(self.store.get(role))))
        return self.manage("rotate_key", {"key_fingerprint": fp}, {role: "public_key"})

    def create_connection(self, profile="coordination", agent_id=None, agent_name=None, label="Machine connection"):
        if not agent_id and not agent_name:
            agent_id = self._state()["connection"]["agent_id"]
        fields = {"profile": profile, "name": label}
        fields.update({"agent_id": agent_id} if agent_id else {"agent_name": agent_name})
        result = self.manage("create_connection", fields, {"work": "work_key"})
        with self.store.lock():
            state = self._state()
            state["profile"] = profile
            self._save(state)
        return result

    def enable_password_login(self, password):
        return self.manage("set_password", {"password": password})

    def recover(self):
        return self.manage("recover", {"connection_id": self._state()["connection"]["client_id"]},
                           {"management": "management_key", "work": "work_key"}, recovery=True)

    def _backup(self, state, path):
        value = {"base_url": self.base, "recovery_key": self.store.get("recovery"),
                 "connection": state.get("connection"), "registration": state.get("registration")}
        key = wrapping_key("AGENTBOARD_BACKUP_KEY")
        nonce = secrets.token_bytes(12)
        atomic_write(path, b"ABRECOVERY1:" + nonce + AESGCM(key).encrypt(nonce, compact(value).encode(), b"AgentBoard recovery v1"))

    def backup(self, path):
        with self.store.lock():
            self._backup(self._state(), path)
        return {"encrypted_recovery_backup": str(Path(path).absolute()), "independent_storage_required": True}

    def restore(self, path):
        raw = Path(path).read_bytes()
        if not raw.startswith(b"ABRECOVERY1:"):
            raise ClientError("Invalid recovery backup")
        value = json.loads(AESGCM(wrapping_key("AGENTBOARD_BACKUP_KEY")).decrypt(raw[12:24], raw[24:], b"AgentBoard recovery v1"))
        if value["base_url"] != self.base:
            raise ClientError("Backup does not contain a completed connection for this server")
        with self.store.lock():
            if self._state():
                raise ClientError("Restore into an empty credential directory")
            self.store.put("recovery", value["recovery_key"])
            if not value.get("connection"):
                fp = digest(compact(public_key(value["recovery_key"])))
                info = self._signed({"account_id": "", "operation_id": "resolve-" + secrets.token_hex(16), "action": "resolve"}, "recovery", fp, "/v1/machine/manage")
                connections = [c for c in info["connections"] if c["revoked_at"] is None]
                if not connections:
                    raise ClientError("No registered connection found for the recovery key")
                value["connection"] = {**connections[0], "account_id": info["account_id"]}
            self._save({"base_url": self.base, "backend": self.store.backend, "connection": value["connection"]})
        return self.recover()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://agentsknow.app")
    parser.add_argument("--state-dir")
    parser.add_argument("--backend", choices=("auto", "file", "keyring", "dpapi"), default="auto")
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect")
    connect.add_argument("--name", default="Agent")
    connect.add_argument("--profile", choices=("memory", "coordination", "full"))
    connect.add_argument("--accept-terms", action="store_true")
    connect.add_argument("--recovery-backup")
    sub.add_parser("info")
    sub.add_parser("connections")
    add = sub.add_parser("create-connection")
    agent = add.add_mutually_exclusive_group()
    agent.add_argument("--agent-id")
    agent.add_argument("--agent-name")
    add.add_argument("--profile", choices=("memory", "coordination", "full"), default="coordination")
    add.add_argument("--label", default="Machine connection")
    sub.add_parser("password-login", help="Optional human login; read password from stdin")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("role", choices=("work", "management", "recovery"), nargs="?", default="work")
    for name in ("backup", "restore"):
        p = sub.add_parser(name)
        p.add_argument("path")
    sub.add_parser("recover")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("client_id", nargs="?")
    sub.add_parser("close-account")
    call = sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("--arguments-file", help="JSON file; use - to read stdin")
    sub.add_parser("mcp-proxy", help="Line-delimited JSON-RPC stdio adapter; never exposes tokens")
    args = parser.parse_args()
    try:
        client = AgentBoardClient(args.base_url, args.state_dir, args.backend)
        if args.command == "connect":
            result = client.connect(args.name, args.profile, args.accept_terms, args.recovery_backup)
        elif args.command == "info":
            result = client.connection_info()
        elif args.command == "connections":
            result = client.manage("list")
        elif args.command == "create-connection":
            result = client.create_connection(args.profile, args.agent_id, args.agent_name, args.label)
        elif args.command == "password-login":
            result = client.enable_password_login(sys.stdin.readline().rstrip("\r\n"))
        elif args.command == "rotate":
            result = client.rotate(args.role)
        elif args.command in ("backup", "restore"):
            result = getattr(client, args.command)(args.path)
        elif args.command == "recover":
            result = client.recover()
        elif args.command == "revoke":
            result = client.manage("revoke_connection", {"connection_id": args.client_id or client._state()["connection"]["client_id"]})
        elif args.command == "close-account":
            result = client.manage("close_account")
        elif args.command == "call":
            data = {}
            if args.arguments_file:
                data = json.loads(sys.stdin.read() if args.arguments_file == "-" else Path(args.arguments_file).read_text(encoding="utf-8"))
            result = client.call_tool(args.tool, data)
        else:
            for line in sys.stdin:
                message = json.loads(line)
                if "id" not in message:
                    continue  # Stateless HTTP server does not require notifications.
                try:
                    response = client.mcp(message)
                except (APIError, RuntimeError):
                    response = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "AgentBoard connection failed; inspect connection status"}}
                print(compact(response), flush=True)
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except APIError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except ClientError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError, KeyError, urllib.error.URLError, InvalidTag, UnsupportedAlgorithm):
        print("AgentBoard client failed; check the saved connection, credential store and network. No credentials were printed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
