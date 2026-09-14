from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

CloudRegion = Literal["global", "china"]
CredentialSource = Literal["manual", "bambu_studio"]


class CredentialStoreError(RuntimeError):
    """Raised when cloud credentials cannot be stored or read safely."""


class CredentialProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class CloudCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    region: CloudRegion
    source: CredentialSource | None = None
    account_fingerprint: str | None = Field(default=None, exclude=True, repr=False)
    account_hint: str | None = Field(default=None, exclude=True)

    def masked_dump(self) -> dict[str, str]:
        return {"access_token": "********", "region": self.region}


class CloudCredentialStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configured: bool
    region: CloudRegion | None = None
    source: CredentialSource | None = None
    account_hint: str | None = None
    protection: Literal["windows-dpapi-current-user"] = "windows-dpapi-current-user"


CredentialStatus = CloudCredentialStatus

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _credential_path_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.absolute()))
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDPAPIProtector:
    """Protect values with Windows DPAPI scoped to the current user."""

    _entropy = b"printing-agent:bambu-cloud:v1"
    _ui_forbidden = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows DPAPI is unavailable on this platform")

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(value, len(value))
        blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def protect(self, value: bytes) -> bytes:
        value_blob, value_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        if not crypt32.CryptProtectData(
            ctypes.byref(value_blob),
            "Bambu Cloud access token",
            ctypes.byref(entropy_blob),
            None,
            None,
            self._ui_forbidden,
            ctypes.byref(output),
        ):
            raise CredentialStoreError("Windows DPAPI could not protect the cloud credential")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(value_buffer, 0, len(value_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
            self._local_free(output.pbData)

    def unprotect(self, value: bytes) -> bytes:
        value_blob, value_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        if not crypt32.CryptUnprotectData(
            ctypes.byref(value_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            self._ui_forbidden,
            ctypes.byref(output),
        ):
            raise CredentialStoreError("Windows DPAPI could not read the cloud credential")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(value_buffer, 0, len(value_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
            self._local_free(output.pbData)

    @staticmethod
    def _local_free(pointer: ctypes.POINTER(ctypes.c_ubyte)) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))


def default_credential_path() -> Path:
    if os.name != "nt":
        raise CredentialStoreError("Bambu Cloud credentials require Windows DPAPI")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise CredentialStoreError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "PrintingAgent" / "bambu-cloud-credentials.json"


def _restrict_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    identity = (
        f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}"
        if os.environ.get("USERDOMAIN") and os.environ.get("USERNAME")
        else os.environ.get("USERNAME")
    )
    if not identity:
        raise CredentialStoreError("The current Windows identity is unavailable")
    command = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{identity}:(F)",
        "SYSTEM:(F)",
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialStoreError("Could not restrict the cloud credential file ACL") from exc


class CloudCredentialStore:
    """File-backed access-token storage protected by Windows DPAPI."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        protector: CredentialProtector | None = None,
        acl_restrictor: Callable[[Path], None] = _restrict_windows_acl,
    ) -> None:
        if protector is None:
            protector = WindowsDPAPIProtector()
        self._protector = protector
        self.path = path or default_credential_path()
        self._acl_restrictor = acl_restrictor
        self._lock = _credential_path_lock(self.path)

    def store(
        self,
        access_token: str | SecretStr,
        region: CloudRegion,
        *,
        source: CredentialSource = "manual",
        account_fingerprint: str | None = None,
        account_hint: str | None = None,
    ) -> CloudCredentialStatus:
        if region not in {"global", "china"}:
            raise CredentialStoreError("Cloud region must be global or china")
        if source not in {"manual", "bambu_studio"}:
            raise CredentialStoreError("Cloud credential source is invalid")
        raw = (
            access_token.get_secret_value()
            if isinstance(access_token, SecretStr)
            else access_token
        ).strip()
        if not raw:
            raise CredentialStoreError("The access token cannot be empty")
        if source == "manual":
            account_fingerprint = None
            account_hint = None
        if account_fingerprint is not None and (
            len(account_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in account_fingerprint)
        ):
            raise CredentialStoreError("Cloud credential account metadata is invalid")
        if account_hint is not None:
            account_hint = account_hint.strip()
            if not account_hint or len(account_hint) > 255:
                raise CredentialStoreError("Cloud credential account metadata is invalid")

        with self._lock:
            payload = bytearray(
                json.dumps(
                    {
                        "access_token": raw,
                        "region": region,
                        "source": source,
                        "account_fingerprint": account_fingerprint,
                        "account_hint": account_hint,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            try:
                protected = self._protector.protect(bytes(payload))
            finally:
                payload[:] = b"\x00" * len(payload)
            document = {
                "version": 2,
                "encrypted_payload": base64.b64encode(protected).decode("ascii"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(
                f".{self.path.name}.{uuid4().hex}.pending"
            )
            try:
                pending.write_text(
                    json.dumps(document, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                self._acl_restrictor(pending)
                pending.replace(self.path)
            except Exception as exc:
                pending.unlink(missing_ok=True)
                if isinstance(exc, CredentialStoreError):
                    raise
                raise CredentialStoreError(
                    "Could not store the cloud credential safely"
                ) from exc
        return CloudCredentialStatus(
            configured=True,
            region=region,
            source=source,
            account_hint=account_hint,
        )

    def load(self) -> CloudCredentials:
        with self._lock:
            return self._load()

    def _load(self) -> CloudCredentials:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError
            version = document.get("version")
            if version == 1:
                return self._load_v1(document)
            if version != 2 or set(document) != {"version", "encrypted_payload"}:
                raise ValueError
            encrypted = base64.b64decode(document["encrypted_payload"], validate=True)
            decrypted = bytearray(self._protector.unprotect(encrypted))
            try:
                payload = json.loads(decrypted.decode("utf-8"))
            finally:
                decrypted[:] = b"\x00" * len(decrypted)
            if not isinstance(payload, dict) or set(payload) != {
                "access_token",
                "region",
                "source",
                "account_fingerprint",
                "account_hint",
            }:
                raise ValueError
            token = payload["access_token"]
            region = payload["region"]
            source = payload["source"]
            account_fingerprint = payload["account_fingerprint"]
            account_hint = payload["account_hint"]
            if (
                not isinstance(token, str)
                or not token
                or region not in {"global", "china"}
                or source not in {"manual", "bambu_studio"}
                or (
                    account_fingerprint is not None
                    and (
                        not isinstance(account_fingerprint, str)
                        or len(account_fingerprint) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in account_fingerprint
                        )
                    )
                )
                or (
                    account_hint is not None
                    and (
                        not isinstance(account_hint, str)
                        or not account_hint
                        or len(account_hint) > 255
                    )
                )
                or (
                    source == "manual"
                    and (account_fingerprint is not None or account_hint is not None)
                )
            ):
                raise ValueError
            return CloudCredentials(
                access_token=SecretStr(token),
                region=region,
                source=source,
                account_fingerprint=account_fingerprint,
                account_hint=account_hint,
            )
        except FileNotFoundError as exc:
            raise CredentialStoreError("No Bambu Cloud credential is configured") from exc
        except CredentialStoreError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("The cloud credential file is invalid") from exc

    def _load_v1(self, document: object) -> CloudCredentials:
        if not isinstance(document, dict) or set(document) != {
            "version",
            "region",
            "encrypted_access_token",
        }:
            raise ValueError
        if document["region"] not in {"global", "china"}:
            raise ValueError
        encrypted = base64.b64decode(
            document["encrypted_access_token"],
            validate=True,
        )
        decrypted = bytearray(self._protector.unprotect(encrypted))
        try:
            token = decrypted.decode("utf-8")
        finally:
            decrypted[:] = b"\x00" * len(decrypted)
        if not token:
            raise ValueError
        return CloudCredentials(
            access_token=SecretStr(token),
            region=document["region"],
        )

    def clear(self) -> CloudCredentialStatus:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
                self.path.with_suffix(f"{self.path.suffix}.pending").unlink(
                    missing_ok=True
                )
            except OSError as exc:
                raise CredentialStoreError(
                    "Could not clear the cloud credential"
                ) from exc
        return CloudCredentialStatus(configured=False)

    def status(self) -> CloudCredentialStatus:
        with self._lock:
            if not self.path.is_file():
                return CloudCredentialStatus(configured=False)
            try:
                credentials = self._load()
                return CloudCredentialStatus(
                    configured=True,
                    region=credentials.region,
                    source=credentials.source,
                    account_hint=credentials.account_hint,
                )
            except CredentialStoreError:
                return CloudCredentialStatus(configured=False)
