"""Phase 16 - narrow DPAPI (Data Protection API) wrapper, current-user scoped, built on nothing
but ctypes. Thermal Watch's whole design principle is "dependency-free" (see app.py's own module
docstring, and there is no keyring/pywin32/cryptography anywhere in this repo) so this hand-rolls
the three Win32 calls the same way every other Windows-API touchpoint in this codebase already
does (MEMORYSTATUSEX, PDH_FMT_VALUE in app.py) rather than adding a new dependency.

protect()/unprotect() encrypt/decrypt a short blob (an AI provider API key) for "this Windows
user, on this machine" - CryptProtectData ties the ciphertext to the calling user's DPAPI master
key, so a copied config file's credential_ref is unreadable on another account or another
machine. That is a feature (see ai_settings.py: a credential_ref that fails to unprotect() is
treated exactly like any other invalid config - fails safe to disabled, never raises out).

CRYPTPROTECT_UI_FORBIDDEN is passed on every call so this can never block on a modal Windows
credential prompt - important both for headless verification and for an unattended monitoring
app that must never freeze waiting on user interaction it didn't ask for.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class DPAPIError(RuntimeError):
    """A CryptProtectData/CryptUnprotectData call failed (wrong user/machine, corrupted blob,
    or any other DPAPI-reported failure). Callers must treat this the same as any other invalid-
    config condition - see ai_settings.load_provider_config()'s blanket except clause."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


# use_last_error=True is required for ctypes.get_last_error() below to report anything
# meaningful - plain ctypes.windll.* does not track it.
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# CRITICAL (well-known ctypes correctness pitfall on 64-bit Windows): the default argument/return
# marshaling for an undeclared ctypes function is `int` (32-bit), which silently truncates any
# pointer-sized value - DATA_BLOB* and the LocalFree HLOCAL return included. Every argument and
# the return type are declared explicitly below using the real Win32 signatures rather than left
# to ctypes' default guess.
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),   # pDataIn
    wintypes.LPCWSTR,            # szDataDescr
    ctypes.POINTER(DATA_BLOB),   # pOptionalEntropy
    wintypes.LPVOID,             # pvReserved
    wintypes.LPVOID,             # pPromptStruct (CRYPTPROTECT_PROMPTSTRUCT*)
    wintypes.DWORD,              # dwFlags
    ctypes.POINTER(DATA_BLOB),   # pDataOut
]
_crypt32.CryptProtectData.restype = wintypes.BOOL

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),   # pDataIn
    ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr
    ctypes.POINTER(DATA_BLOB),   # pOptionalEntropy
    wintypes.LPVOID,             # pvReserved
    wintypes.LPVOID,             # pPromptStruct
    wintypes.DWORD,              # dwFlags
    ctypes.POINTER(DATA_BLOB),   # pDataOut
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

_kernel32.LocalFree.argtypes = [wintypes.LPVOID]
_kernel32.LocalFree.restype = wintypes.LPVOID

CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _local_free(ptr):
    if ptr:
        _kernel32.LocalFree(ctypes.cast(ptr, wintypes.LPVOID))


def protect(data: bytes) -> bytes:
    """DPAPI-encrypt `data` for the current Windows user. Returns raw encrypted bytes (the
    caller, ai_settings.py, base64-encodes these for JSON storage - this function itself only
    ever deals in bytes)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("protect() requires bytes")
    raw = bytes(data)
    # Held alive as `buffer` for the entire duration of the call below - not a temporary that
    # could be garbage-collected mid-call, per the ctypes-lifetime pitfall this module must avoid.
    buffer = ctypes.create_string_buffer(raw, len(raw)) if raw else ctypes.create_string_buffer(0)
    in_blob = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )
    if not ok:
        raise DPAPIError(f"CryptProtectData failed (Win32 error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _local_free(out_blob.pbData)


def unprotect(blob: bytes) -> bytes:
    """Reverse of protect(). Raises DPAPIError on failure (wrong user/machine, corrupted blob,
    etc.) - callers must not let that escape uncaught; see ai_settings.py's load path, which
    treats it exactly like any other invalid-config condition."""
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("unprotect() requires bytes")
    raw = bytes(blob)
    if not raw:
        raise DPAPIError("unprotect() requires a non-empty blob")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    in_blob = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )
    if not ok:
        raise DPAPIError(f"CryptUnprotectData failed (Win32 error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _local_free(out_blob.pbData)
