from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

CloudRegion = Literal["global", "china"]


class CredentialStoreError(RuntimeError):
    """Raised when cloud credentials cannot be stored or read safely."""


class CredentialProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class CloudCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    region: CloudRegion

    def masked_dump(self) -> dict[str, str]:
        return {"access_token": "********", "region": self.region}


class CloudCredentialStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configured: bool
    region: CloudRegion | None = None
    protection: Literal["windows-dpapi-current-user"] = "windows-dpapi-current-user"


CredentialStatus = CloudCredentialStatus


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

    def store(
        self,
        access_token: str | SecretStr,
        region: CloudRegion,
    ) -> CloudCredentialStatus:
        if region not in {"global", "china"}:
            raise CredentialStoreError("Cloud region must be global or china")
        raw = (
            access_token.get_secret_value()
            if isinstance(access_token, SecretStr)
            else access_token
        ).strip()
        if not raw:
            raise CredentialStoreError("The access token cannot be empty")

        protected = self._protector.protect(raw.encode("utf-8"))
        document = {
            "version": 1,
            "region": region,
            "encrypted_access_token": base64.b64encode(protected).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(f"{self.path.suffix}.pending")
        try:
            pending.write_text(
                json.dumps(document, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            pending.replace(self.path)
            self._acl_restrictor(self.path)
        except Exception as exc:
            pending.unlink(missing_ok=True)
            self.path.unlink(missing_ok=True)
            if isinstance(exc, CredentialStoreError):
                raise
            raise CredentialStoreError("Could not store the cloud credential safely") from exc
        return CloudCredentialStatus(configured=True, region=region)

    def load(self) -> CloudCredentials:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if set(document) != {
                "version",
                "region",
                "encrypted_access_token",
            }:
                raise ValueError
            if document["version"] != 1 or document["region"] not in {"global", "china"}:
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
        except FileNotFoundError as exc:
            raise CredentialStoreError("No Bambu Cloud credential is configured") from exc
        except CredentialStoreError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("The cloud credential file is invalid") from exc

    def clear(self) -> CloudCredentialStatus:
        try:
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(f"{self.path.suffix}.pending").unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("Could not clear the cloud credential") from exc
        return CloudCredentialStatus(configured=False)

    def status(self) -> CloudCredentialStatus:
        if not self.path.is_file():
            return CloudCredentialStatus(configured=False)
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            region = document.get("region")
            encrypted = document.get("encrypted_access_token")
            if (
                document.get("version") != 1
                or region not in {"global", "china"}
                or not isinstance(encrypted, str)
                or not encrypted
            ):
                raise ValueError
            base64.b64decode(encrypted, validate=True)
            return CloudCredentialStatus(configured=True, region=region)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return CloudCredentialStatus(configured=False)
