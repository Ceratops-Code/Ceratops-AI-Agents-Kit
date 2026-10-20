"""Dependency-free CLI bootstrap and a persistent MCP stdio gateway.

The same standalone file is installed below every MCP server's ``bin``
directory. It derives that server's identity from the parent directory and checks
the selected receipt before dispatch. MCP connections survive selection changes:
new requests use the selected environment, and older children drain naturally.
The standalone bootstrap cannot import a versioned SDK without pinning that
environment itself. A small JSON-RPC relay negotiates child connections and
gives each backend its own request, progress and callback IDs.
"""

import contextlib
import ctypes
import json
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path


@contextlib.contextmanager
def shared_usage_lease(stream):
    """Hold a shared kernel lease while one selected MCP server is alive."""
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel.UnlockFileEx.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlap = Overlapped()
        if not kernel.LockFileEx(handle, 0, 0, 1, 0, ctypes.byref(overlap)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        try:
            yield
        finally:
            if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error))
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def checked_path(root, path):
    """Reject links and escapes before opening deployment-owned paths."""
    if not path.is_relative_to(root):
        raise ValueError("launcher path escaped installation root")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("launcher rejects links and reparse points")
    return path


def selected_environment(root):
    """Read current authoritative selection; check only immutable identity."""
    identity = root.name
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", identity):
        raise ValueError("invalid MCP server identity")
    selected = json.loads(checked_path(root, root / "current.json").read_text())
    if set(selected) != {"schema", "mcp_server_id", "version", "manifest_sha256", "instance", "module"} or selected["schema"] != 1 or selected["mcp_server_id"] != identity:
        raise ValueError("invalid MCP server selection")
    if not isinstance(selected["instance"], str) or not re.fullmatch("[0-9a-f]{32}", selected["instance"]):
        raise ValueError("invalid installation identity")
    if not isinstance(selected["version"], str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", selected["version"]):
        raise ValueError("invalid selected version")
    directory = root / "versions" / selected["version"] / selected["instance"]
    return selected, directory


@contextlib.contextmanager
def leased_command(root, selected, directory, arguments):
    """Hold the environment's lease until its process actually exits."""
    locks = root / "locks"
    locks.mkdir(exist_ok=True)
    checked_path(root, locks)
    lease = locks / f"{selected['instance']}.usage.lock"
    with lease.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        with shared_usage_lease(stream):
            if json.loads(checked_path(root, directory / "receipt.json").read_text()) != selected:
                raise ValueError("MCP server receipt mismatch")
            python = checked_path(root, directory / "environment" / "Scripts" / "python.exe")
            yield [str(python), "-I", "-B", "-m", selected["module"], *arguments]


def child_options():
    return {"env": {k: v for k, v in os.environ.items()
                    if not k.upper().startswith(("PYTHON", "PIP_", "UV_"))},
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0}


def message_key(identifier):
    return json.dumps(identifier, ensure_ascii=True, sort_keys=True)


def redacted_detail(detail):
    for name, value in os.environ.items():
        if len(value) > 3 and re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", name, re.I):
            detail = detail.replace(value, "<redacted>")
    return re.sub(r"(?i)((?:token|password|secret|api[_-]?key)[=:]\s*)\S+", r"\1<redacted>", detail)


def read_messages(stream, events, backend):
    """One reader preserves each stream's order; queues bound pending traffic."""
    try:
        for line in stream:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC frame is not an object")
            events.put(("message", backend, message))
    except (OSError, ValueError) as exc:
        events.put(("read_error", backend, str(exc)))
    finally:
        if backend is not None:
            backend.process.wait()
            backend.error_reader.join(timeout=1)
        events.put(("eof", backend, None))


class Backend:
    """One leased child. Retirement closes input, never kills accepted work."""

    def __init__(self, root, selected, directory, arguments, events, initialize):
        self.selected = selected
        self.events = events
        self.pending = set()
        self.request_ids = {}
        self.wire_ids = {}
        self.progress_tokens = {}
        self.retiring = False
        self.ready = initialize is None
        self.bootstrap_id = None
        self.started = time.monotonic()
        self.errors: deque[bytes] = deque(maxlen=16)
        self.output: queue.Queue[dict | None] = queue.Queue(maxsize=128)
        self.lease = contextlib.ExitStack()
        self.command = []
        try:
            self.command = self.lease.enter_context(leased_command(root, selected, directory, arguments))
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            **child_options())
        except OSError as exc:
            self.lease.close()
            raise OSError(redacted_detail(
                f"start {selected['mcp_server_id']}@{selected['version']}; "
                f"runtime={self.command[0] if self.command else '<not resolved>'}; "
                f"invocation={subprocess.list2cmdline(self.command) if self.command else '<not started>'}; "
                f"exit=not_started; stdout=<absent>; stderr=<absent>; cause={exc}")) from exc
        except BaseException:
            self.lease.close()
            raise
        self.error_reader = threading.Thread(target=self.read_errors, daemon=True)
        self.error_reader.start()
        threading.Thread(target=self.write_messages, daemon=True).start()
        threading.Thread(target=read_messages, args=(self.process.stdout, events, self), daemon=True).start()
        if initialize is not None:
            self.bootstrap_id = "gateway-initialize-" + uuid.uuid4().hex
            self.send({**initialize, "id": self.bootstrap_id})

    def read_errors(self):
        assert self.process.stderr is not None
        while chunk := self.process.stderr.read(4096):
            self.errors.append(chunk)
            stream = getattr(sys.stderr, "buffer", sys.stderr)
            try:
                stream.write(chunk if hasattr(sys.stderr, "buffer") else chunk.decode("utf-8", "replace"))
                stream.flush()
            except (OSError, ValueError):
                pass

    def write_messages(self):
        assert self.process.stdin is not None
        try:
            while True:
                try:
                    message = self.output.get(timeout=0.2)
                except queue.Empty:
                    if self.retiring:
                        break
                    continue
                if message is None:
                    break
                self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self.events.put(("read_error", self, str(exc)))
        finally:
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.close()

    def send(self, message):
        if self.retiring:
            raise ValueError("selected backend is retiring")
        self.output.put_nowait(message)

    def retire(self):
        if not self.retiring:
            self.retiring = True
            with contextlib.suppress(queue.Full):
                self.output.put_nowait(None)

    def forget_request(self, key):
        """Late replies and progress cannot attach to a reused client ID."""
        wire_id = self.wire_ids.pop(key, None)
        self.request_ids.pop(message_key(wire_id), None)
        self.pending.discard(key)
        self.progress_tokens = {token: value for token, value in self.progress_tokens.items()
                                if value[0] != key}
        return wire_id

    def diagnostic(self, cause):
        stderr = b"".join(self.errors).decode("utf-8", "replace") or "<absent>"
        detail = (f"{cause}; target={self.selected['mcp_server_id']}@{self.selected['version']}; "
                  f"invocation={subprocess.list2cmdline(self.command)}; exit={self.process.poll()}; "
                  f"stdout=<JSON-RPC transport>; stderr={stderr}")
        return redacted_detail(detail)


class McpGateway:
    """Route at dispatch, preserving cancellation, progress and back-channel IDs.

    No progress files or history are written. Only active protocol messages and
    64 KiB of child stderr are kept in memory. Idle selection polling releases
    old environments even when a chat never makes another tool call.
    """

    def __init__(self, root, arguments):
        self.root, self.arguments = root, arguments
        self.events: queue.Queue = queue.Queue(maxsize=128)
        self.backends = set()
        self.pending = {}
        self.waiting = {}
        self.waiting_on = {}
        self.callbacks = {}
        self.initialize = None
        self.protocol = None
        self.initialized = False
        self.closing = False
        self.output_closed = False

    def emit(self, message):
        if self.output_closed:
            return
        try:
            data = json.dumps(message, ensure_ascii=False) + "\n"
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(data.encode("utf-8"))
            else:
                sys.stdout.write(data)
            sys.stdout.flush()
        except (OSError, ValueError):
            self.closing = True
            self.output_closed = True

    def error(self, message, detail):
        self.emit({"jsonrpc": "2.0", "id": message["id"],
                   "error": {"code": -32603, "message": "MCP gateway: " + detail}})

    def forward(self, backend, message):
        try:
            backend.send(message)
        except (OSError, ValueError, queue.Full) as exc:
            self.fail_backend(backend, "forward protocol message: " + str(exc))

    def backend(self, selected, directory, first_initialize=False):
        for backend in self.backends:
            if backend.selected == selected and not backend.retiring:
                return backend
        initialize = None
        if not first_initialize and self.initialize is not None:
            initialize = {**self.initialize, "params": {**self.initialize["params"],
                                                       "protocolVersion": self.protocol}}
        backend = Backend(self.root, selected, directory, self.arguments, self.events, initialize)
        self.backends.add(backend)
        return backend

    def dispatch_waiting(self):
        for key, message in list(self.waiting.items()):
            if self.initialize is not None and self.protocol is None and message["method"] != "initialize":
                continue
            backend = None
            try:
                selected, directory = selected_environment(self.root)
                backend = self.backend(selected, directory, message["method"] == "initialize")
                self.waiting_on[key] = backend
                if not backend.ready:
                    continue
                # Initialization may have taken time: do not dispatch against
                # a selection superseded while that child was starting.
                if selected_environment(self.root)[0] != selected:
                    continue
                wire_id = "gateway-request-" + uuid.uuid4().hex
                payload = {**message, "id": wire_id}
                params = message.get("params", {})
                meta = params.get("_meta", {}) if isinstance(params, dict) else {}
                if isinstance(meta, dict) and "progressToken" in meta:
                    token = "gateway-progress-" + uuid.uuid4().hex
                    payload["params"] = {**params, "_meta": {**meta, "progressToken": token}}
                    backend.progress_tokens[message_key(token)] = (key, meta["progressToken"])
                backend.send(payload)
                backend.request_ids[message_key(wire_id)] = key
                backend.wire_ids[key] = wire_id
                backend.pending.add(key)
                self.pending[key] = (backend, message)
            except (OSError, ValueError, queue.Full) as exc:
                if backend is not None:
                    backend.forget_request(key)
                self.error(message, f"dispatch {message['method']} for {self.root.name}: {exc}; "
                           "stdout=<request not dispatched>; stderr=<not started>")
            self.waiting.pop(key, None)
            self.waiting_on.pop(key, None)

    def client_message(self, message):
        method = message.get("method")
        if method is None:
            callback = self.callbacks.pop(message_key(message.get("id")), None)
            if callback:
                backend, original = callback
                self.forward(backend, {**message, "id": original})
            return
        if "id" in message:
            key = message_key(message["id"])
            if key in self.pending or key in self.waiting:
                self.error(message, "duplicate active request ID")
                return
            if len(self.pending) + len(self.waiting) >= 128:
                self.error(message, "128 active requests are already pending; request not dispatched")
                return
            if method == "initialize":
                if self.initialize is not None:
                    self.error(message, "connection is already initialized")
                    return
                self.initialize = dict(message)
            self.waiting[key] = message
            return
        if method == "notifications/initialized":
            self.initialized = True
        if method == "notifications/cancelled":
            key = message_key(message.get("params", {}).get("requestId"))
            pending = self.pending.pop(key, None)
            if pending:
                owner, request = pending
                wire_id = owner.forget_request(key)
                self.forward(owner, {**message, "params": {**message["params"], "requestId": wire_id}})
                self.emit({"jsonrpc": "2.0", "id": request["id"], "error": {
                    "code": -32800, "message": "client cancelled request; backend cancellation forwarded"}})
            else:
                self.waiting.pop(key, None)
                self.waiting_on.pop(key, None)
            return
        for backend in self.backends:
            if backend.ready and not backend.retiring:
                self.forward(backend, message)

    def backend_message(self, backend, message):
        if message.get("id") == backend.bootstrap_id and backend.bootstrap_id is not None:
            result = message.get("result", {})
            if "error" in message or result.get("protocolVersion") != self.protocol:
                self.fail_backend(backend, "replacement initialization failed: " + json.dumps(message))
                return
            backend.ready = True
            if self.initialized:
                self.forward(backend, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            if self.initialized and result.get("capabilities", {}).get("tools") is not None:
                self.emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            return
        if "method" in message:
            if message["method"] == "notifications/progress":
                progress = backend.progress_tokens.get(message_key(message.get("params", {}).get("progressToken")))
                if progress is None:
                    return
                message = {**message, "params": {**message["params"], "progressToken": progress[1]}}
            if message["method"] == "notifications/cancelled":
                original = message.get("params", {}).get("requestId")
                for identifier, (owner, request_id) in list(self.callbacks.items()):
                    if owner is backend and request_id == original:
                        message = {**message, "params": {**message["params"],
                                   "requestId": json.loads(identifier)}}
                        self.callbacks.pop(identifier)
                        break
            if "id" in message:
                if self.closing:
                    self.forward(backend, {"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32000, "message": "MCP client input closed; callback not dispatched"}})
                    return
                if len(self.callbacks) >= 128:
                    self.forward(backend, {"jsonrpc": "2.0", "id": message["id"],
                                          "error": {"code": -32603, "message": "gateway back-channel capacity exceeded"}})
                    return
                identifier = "gateway-callback-" + uuid.uuid4().hex
                self.callbacks[message_key(identifier)] = (backend, message["id"])
                message = {**message, "id": identifier}
            self.emit(message)
            return
        key = backend.request_ids.pop(message_key(message.get("id")), None)
        pending = self.pending.get(key)
        if not pending or pending[0] is not backend:
            return
        self.pending.pop(key)
        backend.forget_request(key)
        if pending[1]["method"] == "initialize" and "result" in message:
            self.protocol = message["result"].get("protocolVersion")
            tools = message["result"].get("capabilities", {}).get("tools")
            if isinstance(tools, dict):
                tools["listChanged"] = True
        self.emit({**message, "id": pending[1]["id"]})

    def fail_backend(self, backend, cause):
        detail = backend.diagnostic(cause)
        for key, (owner, message) in list(self.pending.items()):
            if owner is backend:
                self.error(message, f"{message['method']} request {message['id']}: {detail}; completion unknown")
                self.pending.pop(key)
                backend.forget_request(key)
        try:
            selected = selected_environment(self.root)[0]
        except (OSError, ValueError):
            selected = None
        if selected == backend.selected:
            for key, message in list(self.waiting.items()):
                if self.waiting_on.get(key) is backend:
                    self.error(message, f"{message['method']} request {message['id']}: {detail}; request not dispatched")
                    self.waiting.pop(key)
                    self.waiting_on.pop(key, None)
        self.callbacks = {key: value for key, value in self.callbacks.items() if value[0] is not backend}
        backend.retire()
        if backend.bootstrap_id is not None and not backend.ready and backend.process.poll() is None:
            # A failed private handshake accepted no application request. It
            # must not retain an environment indefinitely if stdin is ignored.
            try:
                backend.process.kill()
            except OSError as exc:
                print(backend.diagnostic(f"stop failed initialization: {exc}"), file=sys.stderr)

    def retire_idle(self):
        try:
            selected = selected_environment(self.root)[0]
        except (OSError, ValueError):
            selected = None
        for backend in list(self.backends):
            if not backend.ready and not backend.retiring and time.monotonic() - backend.started > 60:
                self.fail_backend(backend, "replacement initialization exceeded 60 seconds")
            busy = (backend.pending or backend in self.waiting_on.values()
                    or any(value[0] is backend for value in self.callbacks.values()))
            if not busy and (self.closing or (selected is not None and backend.selected != selected)):
                backend.retire()

    def run(self):
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        threading.Thread(target=read_messages, args=(stream, self.events, None), daemon=True).start()
        while not self.closing or self.backends:
            try:
                kind, backend, value = self.events.get(timeout=1)
            except queue.Empty:
                if not self.closing:
                    self.dispatch_waiting()
                self.retire_idle()
                continue
            if kind == "message":
                if backend is None:
                    self.client_message(value)
                else:
                    self.backend_message(backend, value)
            elif backend is None:
                self.closing = True
                for message in self.waiting.values():
                    self.error(message, "client input closed before request dispatch")
                self.waiting.clear()
                self.waiting_on.clear()
                for owner, original in list(self.callbacks.values()):
                    self.forward(owner, {"jsonrpc": "2.0", "id": original, "error": {
                        "code": -32000, "message": "MCP client input closed before callback response"}})
                self.callbacks.clear()
            elif kind == "read_error":
                self.fail_backend(backend, value)
            else:
                if (backend.pending or not backend.ready
                        or any(value[0] is backend for value in self.callbacks.values())):
                    self.fail_backend(backend, "selected backend exited before replying")
                backend.retire()
                self.backends.discard(backend)
                backend.lease.close()
                backend.process.stdout.close()
                backend.process.stderr.close()
            if not self.closing:
                self.dispatch_waiting()
            self.retire_idle()
        return 0


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    arguments = sys.argv[1:]
    is_mcp = ("--mcp" in arguments or (not arguments and root.name != "ceratops_mcp_server_manager")
              or (arguments and arguments[0] == "serve"))
    if is_mcp:
        return McpGateway(root, arguments).run()
    selected, directory = selected_environment(root)
    with leased_command(root, selected, directory, arguments) as command:
        # CLI commands retain inherited stdio and their exact child exit code.
        return subprocess.call(command, **child_options(), stdin=sys.stdin,
                               stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"MCP server manager launch failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
