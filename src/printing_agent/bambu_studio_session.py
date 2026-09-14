from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from printing_agent.cloud_credentials import CloudRegion

SessionErrorCode = Literal[
    "studio_not_installed",
    "session_missing",
    "not_signed_in",
    "session_unreadable",
    "session_format_unsupported",
    "region_conflict",
    "region_unsupported",
    "studio_launch_failed",
]
RegionSource = Literal["session", "studio_config"]


class BambuStudioSessionError(RuntimeError):
    """Raised when an official Bambu Studio session cannot be used safely."""

    def __init__(self, code: SessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BambuStudioSessionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: bool
    running: bool
    session_present: bool
    signed_in: bool
    region: CloudRegion | None = None
    region_source: RegionSource | None = None
    account_hint: str | None = None
    session_updated_at: datetime | None = None
    error_code: SessionErrorCode | None = None
    message: str


class ImportedBambuStudioSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    region: CloudRegion
    region_source: RegionSource
    session_ref: str
    account_hint: str | None = None
    account_fingerprint: str | None = Field(default=None, exclude=True, repr=False)
    session_updated_at: datetime


def default_bambu_studio_config_dir() -> Path:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise BambuStudioSessionError(
            "session_unreadable",
            "The Bambu Studio configuration directory is unavailable",
        )
    return Path(app_data) / "BambuStudio"


def _mask_account(account: str | None) -> str | None:
    if not account:
        return None
    value = account.strip()
    if not value:
        return None
    if "@" in value:
        local, domain = value.rsplit("@", 1)
        if local and domain:
            return f"{local[0]}***@{domain}"
    return f"***{value[-4:]}" if len(value) > 4 else "****"


def _default_process_detector(executable: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            [
                "tasklist.exe",
                "/FI",
                f"IMAGENAME eq {executable.name}",
                "/FO",
                "CSV",
                "/NH",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any(
        row and row[0].casefold() == executable.name.casefold()
        for row in csv.reader(result.stdout.splitlines())
    )


def _default_launcher(executable: Path) -> None:
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise BambuStudioSessionError(
            "studio_launch_failed",
            "Bambu Studio could not be opened",
        ) from exc


class BambuStudioSessionAdapter:
    """Imports the signed-in session created by the official Bambu Studio client."""

    _session_filename = "BambuNetworkEngine.conf"
    _studio_config_filename = "BambuStudio.conf"
    _format_key = b"i4crL3LESLnWapLS"
    _max_ciphertext_bytes = 1024 * 1024
    _max_config_bytes = 2 * 1024 * 1024
    _stable_read_attempts = 3

    def __init__(
        self,
        executable_path: str | Path | None,
        config_dir: Path | None = None,
        *,
        process_detector: Callable[[Path], bool] = _default_process_detector,
        launcher: Callable[[Path], None] = _default_launcher,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.executable_path = Path(executable_path) if executable_path else None
        self.config_dir = config_dir
        self._process_detector = process_detector
        self._launcher = launcher
        self._sleep = sleep

    @property
    def session_path(self) -> Path:
        config_dir = self.config_dir or default_bambu_studio_config_dir()
        return config_dir / self._session_filename

    @property
    def studio_config_path(self) -> Path:
        config_dir = self.config_dir or default_bambu_studio_config_dir()
        return config_dir / self._studio_config_filename

    def status(self) -> BambuStudioSessionStatus:
        return self.inspect()[0]

    def inspect(
        self,
    ) -> tuple[BambuStudioSessionStatus, ImportedBambuStudioSession | None]:
        executable = self._validated_executable(required=False)
        installed = executable is not None
        running = bool(executable and self._process_detector(executable))
        try:
            session_path = self.session_path
        except BambuStudioSessionError as exc:
            return (
                BambuStudioSessionStatus(
                    installed=installed,
                    running=running,
                    session_present=False,
                    signed_in=False,
                    error_code=exc.code,
                    message=exc.message,
                ),
                None,
            )

        session_present = session_path.is_file() and not session_path.is_symlink()
        try:
            updated_at = self._updated_at(session_path) if session_present else None
        except BambuStudioSessionError as exc:
            return (
                BambuStudioSessionStatus(
                    installed=installed,
                    running=running,
                    session_present=session_present,
                    signed_in=False,
                    error_code=exc.code,
                    message=exc.message,
                ),
                None,
            )
        if not session_present:
            return (
                BambuStudioSessionStatus(
                    installed=installed,
                    running=running,
                    session_present=False,
                    signed_in=False,
                    session_updated_at=updated_at,
                    error_code="session_missing",
                    message="Sign in with Bambu Studio, then check the session again",
                ),
                None,
            )
        try:
            session = self.read_signed_in_session()
        except BambuStudioSessionError as exc:
            return (
                BambuStudioSessionStatus(
                    installed=installed,
                    running=running,
                    session_present=True,
                    signed_in=False,
                    session_updated_at=updated_at,
                    error_code=exc.code,
                    message=exc.message,
                ),
                None,
            )
        return (
            BambuStudioSessionStatus(
                installed=installed,
                running=running,
                session_present=True,
                signed_in=True,
                region=session.region,
                region_source=session.region_source,
                account_hint=session.account_hint,
                session_updated_at=session.session_updated_at,
                message="Bambu Studio is signed in and ready to import",
            ),
            session,
        )

    def open(self) -> BambuStudioSessionStatus:
        executable = self._validated_executable(required=True)
        assert executable is not None
        if not self._process_detector(executable):
            self._launcher(executable)
        status = self.status()
        return status.model_copy(update={"running": True})

    def read_signed_in_session(self) -> ImportedBambuStudioSession:
        path = self.session_path
        ciphertext = self._read_stable(path)
        plaintext = bytearray()
        try:
            try:
                decryptor = Cipher(
                    algorithms.AES(self._format_key),
                    modes.ECB(),
                ).decryptor()
                plaintext.extend(decryptor.update(bytes(ciphertext)))
                plaintext.extend(decryptor.finalize())
            except ValueError as exc:
                raise BambuStudioSessionError(
                    "session_format_unsupported",
                    "The Bambu Studio session format is unsupported",
                ) from exc
            end = len(plaintext)
            while end and plaintext[end - 1] == 0:
                end -= 1
            if end == 0:
                raise BambuStudioSessionError(
                    "session_format_unsupported",
                    "The Bambu Studio session format is unsupported",
                )
            try:
                document = json.loads(bytes(plaintext[:end]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BambuStudioSessionError(
                    "session_format_unsupported",
                    "The Bambu Studio session format is unsupported",
                ) from exc
            return self._parse_document(document, self._updated_at(path))
        finally:
            ciphertext[:] = b"\x00" * len(ciphertext)
            plaintext[:] = b"\x00" * len(plaintext)

    def _validated_executable(self, *, required: bool) -> Path | None:
        path = self.executable_path
        valid = (
            path is not None
            and path.name.casefold() == "bambu-studio.exe"
            and path.is_file()
            and not path.is_symlink()
            and stat.S_ISREG(path.stat().st_mode)
        )
        if valid:
            return path
        if required:
            raise BambuStudioSessionError(
                "studio_not_installed",
                "Bambu Studio is not installed or configured on this server",
            )
        return None

    def _read_stable(self, path: Path) -> bytearray:
        return self._read_stable_file(
            path,
            max_bytes=self._max_ciphertext_bytes,
            require_aes_blocks=True,
            missing_code="session_missing",
            missing_message="Sign in with Bambu Studio before importing the account",
            invalid_code="session_format_unsupported",
            invalid_message="The Bambu Studio session format is unsupported",
        )

    def _read_stable_file(
        self,
        path: Path,
        *,
        max_bytes: int,
        require_aes_blocks: bool,
        missing_code: SessionErrorCode,
        missing_message: str,
        invalid_code: SessionErrorCode,
        invalid_message: str,
    ) -> bytearray:
        if path.is_symlink():
            raise BambuStudioSessionError(
                "session_unreadable",
                "The Bambu Studio configuration could not be read safely",
            )
        for attempt in range(self._stable_read_attempts):
            try:
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise OSError
                if (
                    before.st_size <= 0
                    or before.st_size > max_bytes
                    or (
                        require_aes_blocks
                        and before.st_size % (algorithms.AES.block_size // 8) != 0
                    )
                ):
                    raise BambuStudioSessionError(
                        invalid_code,
                        invalid_message,
                    )
                value = bytearray(path.read_bytes())
                after = path.stat()
            except FileNotFoundError as exc:
                raise BambuStudioSessionError(
                    missing_code,
                    missing_message,
                ) from exc
            except BambuStudioSessionError:
                raise
            except OSError as exc:
                raise BambuStudioSessionError(
                    "session_unreadable",
                    "The Bambu Studio session could not be read safely",
                ) from exc
            if (
                before.st_size == after.st_size == len(value)
                and before.st_mtime_ns == after.st_mtime_ns
            ):
                return value
            value[:] = b"\x00" * len(value)
            if attempt + 1 < self._stable_read_attempts:
                self._sleep(0.05)
        raise BambuStudioSessionError(
            "session_unreadable",
            "Bambu Studio is updating its configuration; try importing again",
        )

    def _parse_document(
        self,
        document: object,
        updated_at: datetime,
    ) -> ImportedBambuStudioSession:
        if not isinstance(document, dict):
            raise BambuStudioSessionError(
                "session_format_unsupported",
                "The Bambu Studio session format is unsupported",
            )
        user = document.get("user")
        if not isinstance(user, dict):
            raise BambuStudioSessionError(
                "session_format_unsupported",
                "The Bambu Studio session format is unsupported",
            )
        token = user.get("token")
        if not isinstance(token, str) or not token.strip():
            raise BambuStudioSessionError(
                "not_signed_in",
                "Sign in with Bambu Studio before importing the account",
            )
        token = token.strip()
        if len(token) > 4096:
            raise BambuStudioSessionError(
                "session_format_unsupported",
                "The Bambu Studio session format is unsupported",
            )
        country_code = document.get("country_code")
        session_region = self._normalize_region(country_code)
        if (
            isinstance(country_code, str)
            and country_code.strip()
            and session_region is None
        ):
            raise BambuStudioSessionError(
                "region_unsupported",
                "The Bambu Studio session region is unsupported",
            )
        config_region = self._read_studio_config_region()
        if session_region and config_region and session_region != config_region:
            raise BambuStudioSessionError(
                "region_conflict",
                "Bambu Studio session and login regions disagree; reopen Studio and sign in again",
            )
        region = session_region or config_region
        if region is None:
            raise BambuStudioSessionError(
                "region_unsupported",
                "The Bambu Studio account region could not be detected",
            )
        region_source: RegionSource = (
            "session" if session_region is not None else "studio_config"
        )
        account = user.get("account")
        user_id = user.get("user_id")
        account_fingerprint = self._account_fingerprint(
            region,
            user_id if isinstance(user_id, (str, int)) else None,
            account if isinstance(account, str) else None,
        )
        return ImportedBambuStudioSession(
            access_token=SecretStr(token),
            region=region,
            region_source=region_source,
            session_ref=self._session_ref(
                token,
                region,
                account_fingerprint,
                updated_at,
            ),
            account_hint=_mask_account(account if isinstance(account, str) else None),
            account_fingerprint=account_fingerprint,
            session_updated_at=updated_at,
        )

    def _read_studio_config_region(self) -> CloudRegion | None:
        path = self.studio_config_path
        if not path.exists():
            return None
        raw = self._read_stable_file(
            path,
            max_bytes=self._max_config_bytes,
            require_aes_blocks=False,
            missing_code="region_unsupported",
            missing_message="The Bambu Studio login region configuration is unavailable",
            invalid_code="region_unsupported",
            invalid_message="The Bambu Studio login region configuration is invalid",
        )
        try:
            try:
                text = bytes(raw).decode("utf-8-sig")
                document, _ = json.JSONDecoder().raw_decode(text.lstrip())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BambuStudioSessionError(
                    "region_unsupported",
                    "The Bambu Studio login region configuration is invalid",
                ) from exc
            if not isinstance(document, dict):
                raise BambuStudioSessionError(
                    "region_unsupported",
                    "The Bambu Studio login region configuration is invalid",
                )
            app = document.get("app")
            if not isinstance(app, dict):
                return None
            raw_region = app.get("region")
            region = self._normalize_region(raw_region)
            if (
                isinstance(raw_region, str)
                and raw_region.strip()
                and region is None
            ):
                raise BambuStudioSessionError(
                    "region_unsupported",
                    "The Bambu Studio login region is unsupported",
                )
            return region
        finally:
            raw[:] = b"\x00" * len(raw)

    @staticmethod
    def _normalize_region(value: object) -> CloudRegion | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        if normalized in {"cn", "chn", "china"}:
            return "china"
        if normalized in {
            "us",
            "usa",
            "north america",
            "europe",
            "asia-pacific",
            "others",
        }:
            return "global"
        return None

    @staticmethod
    def _account_fingerprint(
        region: CloudRegion,
        user_id: str | int | None,
        account: str | None,
    ) -> str | None:
        identifier: tuple[str, str] | None = None
        if user_id is not None and str(user_id).strip():
            identifier = ("user_id", str(user_id).strip())
        elif account is not None and account.strip():
            identifier = ("account", account.strip().casefold())
        if identifier is None:
            return None
        kind, value = identifier
        return hashlib.sha256(
            f"{region}\0{kind}\0{value}".encode()
        ).hexdigest()

    @staticmethod
    def _session_ref(
        access_token: str,
        region: CloudRegion,
        account_fingerprint: str | None,
        updated_at: datetime,
    ) -> str:
        return hashlib.sha256(
            (
                f"{region}\0{account_fingerprint or ''}\0"
                f"{updated_at.isoformat()}\0{access_token}"
            ).encode()
        ).hexdigest()

    @staticmethod
    def _updated_at(path: Path) -> datetime:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError as exc:
            raise BambuStudioSessionError(
                "session_unreadable",
                "The Bambu Studio session could not be read safely",
            ) from exc
