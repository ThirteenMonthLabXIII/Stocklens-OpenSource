from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from stock_tool.core.cache import CacheStore


AI_API_KEY_NAME = "ai_api_key"
TUSHARE_TOKEN_NAME = "tushare_token"
TICKFLOW_API_KEY_NAME = "tickflow_api_key"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
WINDOWS_CREDENTIAL_PREFIX = "A股选股仪"


class SecretBackendError(RuntimeError):
    pass


class _WindowsCredentialBackend:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretBackendError("仅 Windows 支持凭据管理器")
        self.advapi32 = ctypes.windll.advapi32

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self.CREDENTIALW = CREDENTIALW
        self.PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
        self.advapi32.CredWriteW.argtypes = [self.PCREDENTIALW, wintypes.DWORD]
        self.advapi32.CredWriteW.restype = wintypes.BOOL
        self.advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(self.PCREDENTIALW),
        ]
        self.advapi32.CredReadW.restype = wintypes.BOOL
        self.advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.advapi32.CredDeleteW.restype = wintypes.BOOL
        self.advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self.advapi32.CredFree.restype = None

    def _target(self, name: str) -> str:
        return f"{WINDOWS_CREDENTIAL_PREFIX}/{name}"

    def get(self, name: str) -> str:
        credential_ptr = self.PCREDENTIALW()
        ok = self.advapi32.CredReadW(self._target(name), CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr))
        if not ok:
            return ""
        try:
            credential = credential_ptr.contents
            if not credential.CredentialBlob or credential.CredentialBlobSize <= 0:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self.advapi32.CredFree(credential_ptr)

    def set(self, name: str, value: str) -> None:
        blob = value.encode("utf-16-le")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential = self.CREDENTIALW()
        credential.Flags = 0
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = self._target(name)
        credential.Comment = "A股选股仪 API Key"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.c_void_p)
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = WINDOWS_CREDENTIAL_PREFIX
        ok = self.advapi32.CredWriteW(ctypes.byref(credential), 0)
        if not ok:
            raise SecretBackendError("Windows 凭据管理器写入失败")

    def delete(self, name: str) -> None:
        self.advapi32.CredDeleteW(self._target(name), CRED_TYPE_GENERIC, 0)


class SecretStore:
    """Store sensitive values in Windows Credential Manager when available."""

    def __init__(self, cache: CacheStore, use_keyring: bool = True) -> None:
        self.cache = cache
        self.backend: _WindowsCredentialBackend | None = None
        self.backend_message = "数据文件夹数据库（明文兜底）"
        if use_keyring:
            try:
                self.backend = _WindowsCredentialBackend()
                self.backend_message = "Windows 凭据管理器"
            except Exception:
                self.backend = None

    def get_secret(self, name: str) -> str:
        if self.backend is not None:
            try:
                value = self.backend.get(name)
                if value:
                    self.backend_message = "Windows 凭据管理器"
                    return value
            except Exception:
                self.backend = None
                self.backend_message = "数据文件夹数据库（明文兜底）"
        return self.cache.get_setting(name, "")

    def set_secret(self, name: str, value: str) -> bool:
        clean_value = value.strip()
        if self.backend is not None:
            try:
                if clean_value:
                    self.backend.set(name, clean_value)
                else:
                    self.backend.delete(name)
                self.cache.delete_setting(name)
                self.backend_message = "Windows 凭据管理器"
                return True
            except Exception:
                self.backend = None
        self.cache.set_setting(name, clean_value)
        self.backend_message = "数据文件夹数据库（明文兜底）"
        return False

    def delete_secret(self, name: str) -> None:
        if self.backend is not None:
            try:
                self.backend.delete(name)
            except Exception:
                pass
        self.cache.delete_setting(name)

    def migrate_legacy_secret(self, name: str) -> bool:
        if self.backend is None:
            return False
        legacy_value = self.cache.get_setting(name, "")
        if not legacy_value.strip():
            return False
        try:
            self.backend.set(name, legacy_value.strip())
            self.cache.delete_setting(name)
            self.backend_message = "Windows 凭据管理器（已迁移旧数据库明文）"
            return True
        except Exception:
            return False
