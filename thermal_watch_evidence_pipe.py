"""Local Windows named-pipe transport for Thermal Watch's evidence API.

Transport only - this module has no knowledge of the evidence schema, OPERATIONS catalog, or
snapshot shape (that lives in thermal_watch_evidence_cli.py). It accepts a byte-framed JSON
request, hands the parsed dict to a caller-supplied `request_handler(dict) -> dict`, and writes
back the response as a byte-framed JSON reply. app.py is what wires the two modules together.

Built on `_winapi` (the private-but-stable module CPython's own `multiprocessing` uses for
Windows named pipes) rather than pywin32, matching this codebase's existing zero-third-party-
dependency stance (see ai/secret_store.py). Frame format is custom (4-byte little-endian length
prefix + UTF-8 JSON) rather than multiprocessing.connection's pickle-based protocol, since the
client is PowerShell/.NET, not Python.

PIPE_REJECT_REMOTE_CLIENTS keeps this local-machine-only - no \\\\hostname\\pipe\\ remote access.
"""
from __future__ import annotations

import json
import os
import threading
import time
import _winapi


PIPE_NAME = os.environ.get("THERMAL_WATCH_PIPE_NAME", "ThermalWatchEvidence.v1")
PIPE_PATH = "\\\\.\\pipe\\" + PIPE_NAME

MAX_FRAME_BYTES = 1_048_576  # 1 MiB
LENGTH_PREFIX_SIZE = 4
PIPE_BUFFER_HINT = 8192
CONNECT_POLL_MS = 250
READ_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 5.0
CLIENT_CONNECT_TIMEOUT_S = 2.0
CLIENT_ROUND_TRIP_TIMEOUT_S = 3.0

# Not exposed as a named _winapi constant; literal Win32 value (winbase.h).
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


def _wait(overlapped, deadline):
    """Waits on one overlapped operation's event until it completes or `deadline` passes.
    Returns True if it completed, False on timeout. Mirrors worker()'s stop_event.wait(...)
    polling shape closely enough to reuse the same mental model, but here the thing being waited
    on is a single OS-level I/O completion, not a shutdown flag."""
    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
    result = _winapi.WaitForMultipleObjects([overlapped.event], False, remaining_ms)
    return result != _winapi.WAIT_TIMEOUT


def _read_exact(handle, count, deadline):
    """Reads exactly `count` bytes or raises OSError/TimeoutError. Byte-mode pipes never
    produce ERROR_MORE_DATA (that's a message-mode concept), so a plain accumulate-until-full
    loop is correct and simpler than multiprocessing's own message-mode-aware _recv_bytes."""
    chunks = []
    remaining = count
    while remaining > 0:
        if time.monotonic() >= deadline:
            raise TimeoutError("pipe read timed out")
        ov, _ = _winapi.ReadFile(handle, remaining, overlapped=True)
        if not _wait(ov, deadline):
            ov.cancel()
            raise TimeoutError("pipe read timed out")
        nread, _ = ov.GetOverlappedResult(True)
        chunk = ov.getbuffer()
        if nread == 0:
            raise OSError("pipe closed before the full frame was read")
        chunks.append(chunk[:nread] if chunk is not None else b"")
        remaining -= nread
    return b"".join(chunks)


def _write_all(handle, data, deadline):
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        if time.monotonic() >= deadline:
            raise TimeoutError("pipe write timed out")
        ov, _ = _winapi.WriteFile(handle, bytes(view[offset:]), overlapped=True)
        if not _wait(ov, deadline):
            ov.cancel()
            raise TimeoutError("pipe write timed out")
        nwritten, _ = ov.GetOverlappedResult(True)
        if nwritten == 0:
            raise OSError("pipe closed before the full frame was written")
        offset += nwritten


def _read_frame(handle, deadline):
    length_bytes = _read_exact(handle, LENGTH_PREFIX_SIZE, deadline)
    n = int.from_bytes(length_bytes, "little", signed=False)
    if n == 0 or n > MAX_FRAME_BYTES:
        raise ValueError(f"frame length {n} out of bounds (max {MAX_FRAME_BYTES})")
    return _read_exact(handle, n, deadline)


def _write_frame(handle, payload, deadline):
    _write_all(handle, len(payload).to_bytes(LENGTH_PREFIX_SIZE, "little", signed=False), deadline)
    _write_all(handle, payload, deadline)


class EvidencePipeServer:
    """A local named-pipe server that dispatches framed JSON requests to `request_handler`.

    request_handler: callable(dict) -> dict - given a parsed JSON request, returns a
        JSON-serializable response dict.
    stop_event: threading.Event - reused from the caller (App.stop_event), not a second one,
        matching every other recurring subsystem's shutdown convention in this app.
    log: optional callable(kind, text, meta=None) for lifecycle/error visibility; defaults to a
        no-op so this module has no App dependency and stays independently testable.
    """

    def __init__(self, request_handler, stop_event, log=None):
        self._request_handler = request_handler
        self._stop_event = stop_event
        self._log = log or (lambda kind, text, meta=None: None)
        self._thread = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("EvidencePipeServer already started")
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="EvidencePipeAccept")
        self._thread.start()

    def stop(self, timeout=2.0):
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _accept_loop(self):
        first = True
        while not self._stop_event.is_set():
            try:
                handle = _winapi.CreateNamedPipe(
                    PIPE_PATH,
                    _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
                        | (_winapi.FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
                    _winapi.PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                    _winapi.PIPE_UNLIMITED_INSTANCES,
                    PIPE_BUFFER_HINT, PIPE_BUFFER_HINT,
                    _winapi.NMPWAIT_WAIT_FOREVER, _winapi.NULL,
                )
            except OSError as exc:
                self._log("pipe_error", f"CreateNamedPipe failed: {exc}")
                if self._stop_event.wait(CONNECT_POLL_MS / 1000.0):
                    return
                continue
            first = False
            try:
                connected = self._await_connection(handle)
            except OSError as exc:
                self._log("pipe_error", f"ConnectNamedPipe failed: {exc}")
                _winapi.CloseHandle(handle)
                continue
            if not connected:
                # stop_event fired while waiting for a client - shutting down.
                _winapi.CloseHandle(handle)
                return
            threading.Thread(
                target=self._handle_connection, args=(handle,), daemon=True, name="EvidencePipeConn",
            ).start()

    def _await_connection(self, handle):
        try:
            ov = _winapi.ConnectNamedPipe(handle, overlapped=True)
        except OSError as exc:
            if exc.winerror == _winapi.ERROR_PIPE_CONNECTED:
                return True
            if exc.winerror == _winapi.ERROR_NO_DATA:
                # Client connected, wrote, and disconnected in the race window between
                # CreateNamedPipe and ConnectNamedPipe (see CPython multiprocessing's own
                # PipeListener.accept, bpo-14725). The handle is already usable.
                return True
            raise
        while not self._stop_event.is_set():
            deadline = time.monotonic() + (CONNECT_POLL_MS / 1000.0)
            if _wait(ov, deadline):
                nread, _ = ov.GetOverlappedResult(True)
                return True
        ov.cancel()
        return False

    def _handle_connection(self, handle):
        try:
            deadline = time.monotonic() + READ_TIMEOUT_S
            try:
                payload = _read_frame(handle, deadline)
            except ValueError:
                # Garbage/hostile length prefix: close without reading further and without a
                # response - never allocate a buffer for an unbounded claimed length.
                return
            try:
                request = json.loads(payload.decode("utf-8"))
            except (UnicodeError, ValueError):
                response = {"ok": False, "error": {
                    "code": "malformed_frame",
                    "message": "pipe request frame was not valid UTF-8 JSON",
                }}
            else:
                response = self._request_handler(request)
            body = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            write_deadline = time.monotonic() + WRITE_TIMEOUT_S
            _write_frame(handle, body, write_deadline)
        except Exception as exc:  # noqa: BLE001 - a bad connection must never take down the server
            self._log("pipe_error", f"connection handler failed: {exc}")
        finally:
            try:
                _winapi.CloseHandle(handle)
            except OSError:
                pass


def connect_client(timeout_s=CLIENT_CONNECT_TIMEOUT_S):
    """Opens a client handle to the pipe, or raises OSError promptly if no server is listening.
    Mirrors multiprocessing.connection.PipeClient's retry-on-busy behavior (only
    ERROR_SEM_TIMEOUT/ERROR_PIPE_BUSY are retried, up to timeout_s) but any other error -
    notably a nonexistent pipe - propagates immediately without waiting out the timeout. This is
    the exact mechanism that makes "Thermal Watch isn't running" fail fast instead of hanging.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return _winapi.CreateFile(
                PIPE_PATH,
                _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING, 0, _winapi.NULL,
            )
        except OSError as exc:
            if exc.winerror not in (_winapi.ERROR_SEM_TIMEOUT, _winapi.ERROR_PIPE_BUSY):
                raise
            if time.monotonic() >= deadline:
                raise
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                _winapi.WaitNamedPipe(PIPE_PATH, remaining_ms)
            except OSError:
                raise exc from None


def send_request(handle, request, timeout_s=CLIENT_ROUND_TRIP_TIMEOUT_S):
    """Writes one length-prefixed JSON frame, reads back one, returns the parsed response dict."""
    deadline = time.monotonic() + timeout_s
    body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError("request exceeds the safe size limit")
    _write_frame(handle, body, deadline)
    response_bytes = _read_frame(handle, deadline)
    return json.loads(response_bytes.decode("utf-8"))


def close_client(handle):
    _winapi.CloseHandle(handle)
