from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from printing_agent.artifact_store import sha256_file
from printing_agent.config import Settings
from printing_agent.errors import (
    ConflictError,
    ExternalServiceError,
    PolicyViolationError,
)
from printing_agent.fabrication import (
    BambuConnectHandoff,
    BambuConnectHandoffStatus,
    SlicedArtifact,
)
from printing_agent.fabrication_drivers import BambuStudioCliDriver


class BambuConnectReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: bool
    scheme_registered: bool
    signature_valid: bool
    ready: bool
    message: str
    installation_digest: str | None = None
    signer_thumbprint: str | None = None
    file_version: str | None = None


class BambuConnectManager:
    _ALLOWED_DOWNLOAD_HOSTS = {"public-cdn.bblmw.com"}
    _EXPECTED_SIGNER = "Shanghai Lunkuo Technology Co., Ltd"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.slice_root = settings.slice_dir.resolve()

    def readiness(self) -> BambuConnectReadiness:
        if os.name != "nt":
            return BambuConnectReadiness(
                installed=False,
                scheme_registered=False,
                signature_valid=False,
                ready=False,
                message="Bambu Connect integration currently requires Windows.",
            )
        command = self._registered_command()
        if command is None:
            executable = next(
                (
                    item
                    for item in self._installed_executables()
                    if item.is_file()
                ),
                None,
            )
            if executable is not None:
                signature = self._authenticode(executable)
                valid = self._signature_is_trusted(signature)
                return BambuConnectReadiness(
                    installed=True,
                    scheme_registered=False,
                    signature_valid=valid,
                    ready=False,
                    message=(
                        "Bambu Connect is installed but must be opened once "
                        "to register its URI scheme."
                    ),
                    installation_digest=sha256_file(executable),
                    signer_thumbprint=signature.get("thumbprint"),
                    file_version=signature.get("file_version"),
                )
            return BambuConnectReadiness(
                installed=False,
                scheme_registered=False,
                signature_valid=False,
                ready=False,
                message="Bambu Connect is not installed.",
            )
        executable = self._command_executable(command)
        if executable is None or not executable.is_file():
            return BambuConnectReadiness(
                installed=False,
                scheme_registered=True,
                signature_valid=False,
                ready=False,
                message="Bambu Connect registration points to a missing executable.",
            )
        signature = self._authenticode(executable)
        valid = self._signature_is_trusted(signature)
        return BambuConnectReadiness(
            installed=True,
            scheme_registered=True,
            signature_valid=valid,
            ready=valid,
            message=(
                "Bambu Connect is installed and ready."
                if valid
                else "Bambu Connect has an unexpected or invalid code signature."
            ),
            installation_digest=sha256_file(executable),
            signer_thumbprint=signature.get("thumbprint"),
            file_version=signature.get("file_version"),
        )

    def open(self) -> BambuConnectReadiness:
        readiness = self.readiness()
        if not readiness.installed or not readiness.signature_valid:
            raise ExternalServiceError(readiness.message)
        command = self._registered_command()
        executable = (
            self._command_executable(command or "")
            if command
            else next(
                (
                    item
                    for item in self._installed_executables()
                    if item.is_file()
                ),
                None,
            )
        )
        if executable is None:
            raise ExternalServiceError("Bambu Connect executable is unavailable")
        subprocess.Popen(
            [str(executable)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return readiness

    def install(self) -> BambuConnectReadiness:
        if os.name != "nt":
            raise ExternalServiceError(
                "Bambu Connect installation currently requires Windows"
            )
        url = self.settings.bambu_connect_download_url
        self._validate_download_url(url)
        installer_dir = (self.settings.data_dir / "bambu-connect").resolve()
        installer_dir.mkdir(parents=True, exist_ok=True)
        installer = installer_dir / "bambu-connect-installer.exe"
        try:
            self._download(url, installer)
            signature = self._authenticode(installer)
            if not self._signature_is_trusted(signature):
                raise PolicyViolationError(
                    "Bambu Connect installer signature is invalid or unexpected"
                )
            result = subprocess.run(
                [str(installer), "/S"],
                check=False,
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise ExternalServiceError(
                    f"Bambu Connect installer failed with code {result.returncode}"
                )
            readiness = self.readiness()
            if readiness.installed and not readiness.scheme_registered:
                executable = next(
                    item
                    for item in self._installed_executables()
                    if item.is_file()
                )
                if not self._signature_is_trusted(
                    self._authenticode(executable)
                ):
                    raise PolicyViolationError(
                        "Installed Bambu Connect signature is invalid"
                    )
                subprocess.Popen(
                    [str(executable)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(20):
                    time.sleep(0.5)
                    readiness = self.readiness()
                    if readiness.scheme_registered:
                        break
            if not readiness.ready:
                raise ExternalServiceError(readiness.message)
            return readiness
        except subprocess.TimeoutExpired as exc:
            raise ExternalServiceError("Bambu Connect installation timed out") from exc
        finally:
            installer.unlink(missing_ok=True)

    def prepare_handoff(
        self,
        *,
        workflow_id: str,
        sliced: SlicedArtifact,
        expected_device_ref: str,
        expected_device_name: str,
        attempt: int,
        baseline_state: str,
    ) -> BambuConnectHandoff:
        readiness = self.readiness()
        if not readiness.ready:
            raise ExternalServiceError(readiness.message)
        source = Path(sliced.path).resolve()
        if self.slice_root not in source.parents or not source.is_file():
            raise PolicyViolationError(
                "Sliced artifact is outside the immutable slice root"
            )
        if sha256_file(source) != sliced.digest:
            raise ConflictError("Sliced artifact digest changed before handoff")
        BambuStudioCliDriver._validate_gcode_3mf(source)
        BambuStudioCliDriver._validate_bambu_connect_compatibility(source)
        safe_workflow = re.sub(r"[^A-Za-z0-9_-]+", "_", workflow_id)[:32]
        safe_slice_job = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            sliced.slice_job_id,
        )[:32]
        if not safe_workflow or not safe_slice_job:
            raise PolicyViolationError("Connect handoff identifiers are invalid")
        correlation_name = (
            f"agent-{safe_workflow[:8]}-{safe_slice_job[:8]}-"
            f"{sliced.digest[:8]}-a{attempt}"
        )
        stage_dir = (
            self.slice_root
            / "connect-handoffs"
            / safe_workflow
            / safe_slice_job
            / str(attempt)
        ).resolve()
        if self.slice_root not in stage_dir.parents:
            raise PolicyViolationError("Connect staging path escaped the slice root")
        stage_dir.mkdir(parents=True, exist_ok=False)
        staged = stage_dir / f"{correlation_name}.gcode.3mf"
        try:
            shutil.copyfile(source, staged)
            if sha256_file(staged) != sliced.digest:
                raise ConflictError("Staged Connect artifact digest is invalid")
            query = urlencode(
                {
                    "path": str(staged),
                    "name": correlation_name,
                    "version": "1.0.0",
                }
            )
            return BambuConnectHandoff(
                workflow_id=workflow_id,
                slice_job_id=sliced.slice_job_id,
                sliced_artifact_digest=sliced.digest,
                sliced_manifest_digest=sliced.manifest_digest,
                expected_device_ref=expected_device_ref,
                expected_device_name=expected_device_name,
                staged_path=str(staged),
                correlation_name=correlation_name,
                launch_uri=f"bambu-connect://import-file?{query}",
                attempt=attempt,
                status=BambuConnectHandoffStatus.READY,
                baseline_state=baseline_state,
            )
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def launch(self, handoff: BambuConnectHandoff) -> None:
        parsed = urlsplit(handoff.launch_uri)
        query = parse_qs(parsed.query, strict_parsing=True)
        if (
            parsed.scheme != "bambu-connect"
            or parsed.netloc != "import-file"
            or parsed.path
            or parsed.fragment
            or set(query) != {"path", "name", "version"}
            or any(len(values) != 1 for values in query.values())
            or query["name"][0] != handoff.correlation_name
            or query["version"][0] != "1.0.0"
        ):
            raise PolicyViolationError("Unsupported Bambu Connect launch URI")
        staged = Path(handoff.staged_path).resolve()
        if self.slice_root not in staged.parents or not staged.is_file():
            raise PolicyViolationError("Connect artifact path is unavailable")
        if sha256_file(staged) != handoff.sliced_artifact_digest:
            raise ConflictError("Connect artifact digest changed before launch")
        if Path(query["path"][0]).resolve() != staged:
            raise PolicyViolationError(
                "Bambu Connect launch URI does not reference the verified artifact"
            )
        if not self.readiness().ready:
            raise ExternalServiceError("Bambu Connect is not ready")
        try:
            os.startfile(handoff.launch_uri)  # type: ignore[attr-defined]
        except OSError as exc:
            raise ExternalServiceError("Bambu Connect could not be opened") from exc

    def _download(self, url: str, destination: Path) -> None:
        maximum = self.settings.bambu_connect_download_max_bytes
        total = 0
        current = url
        with httpx.Client(follow_redirects=False, timeout=60) as client:
            for _ in range(4):
                self._validate_download_url(current)
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ExternalServiceError(
                                "Bambu Connect download redirect is invalid"
                            )
                        current = str(response.url.join(location))
                        continue
                    if response.status_code != 200:
                        raise ExternalServiceError(
                            "Bambu Connect installer download failed"
                        )
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > maximum:
                        raise PolicyViolationError(
                            "Bambu Connect installer exceeds the size limit"
                        )
                    with destination.open("xb") as output:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > maximum:
                                raise PolicyViolationError(
                                    "Bambu Connect installer exceeds the size limit"
                                )
                            output.write(chunk)
                    return
        raise ExternalServiceError("Bambu Connect download redirected too many times")

    @classmethod
    def _validate_download_url(cls, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in cls._ALLOWED_DOWNLOAD_HOSTS
            or not parsed.path.lower().endswith(".exe")
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PolicyViolationError("Bambu Connect download URL is not trusted")

    @staticmethod
    def _registered_command() -> str | None:
        if os.name != "nt":
            return None
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT,
                r"bambu-connect\shell\open\command",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            return None
        return value if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _installed_executables() -> tuple[Path, ...]:
        local = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        return tuple(
            path
            for path in (
                (
                    Path(local)
                    / "Programs"
                    / "bambu-connect"
                    / "Bambu Connect.exe"
                )
                if local
                else None,
                (
                    Path(program_files)
                    / "Bambu Connect"
                    / "Bambu Connect.exe"
                )
                if program_files
                else None,
            )
            if path is not None
        )

    @staticmethod
    def _command_executable(command: str) -> Path | None:
        match = re.match(r'^\s*"([^"]+)"|^\s*([^\s]+)', command)
        if match is None:
            return None
        return Path(match.group(1) or match.group(2)).resolve()

    @staticmethod
    def _authenticode(path: Path) -> dict[str, str | None]:
        script = (
            "$s=Get-AuthenticodeSignature "
            "-LiteralPath $env:PRINTING_AGENT_SIGNATURE_PATH;"
            "$v=(Get-Item -LiteralPath "
            "$env:PRINTING_AGENT_SIGNATURE_PATH).VersionInfo.FileVersion;"
            "[PSCustomObject]@{Status=$s.Status.ToString();"
            "Subject=$s.SignerCertificate.Subject;"
            "Thumbprint=$s.SignerCertificate.Thumbprint;"
            "FileVersion=$v}|ConvertTo-Json -Compress"
        )
        shell = shutil.which("pwsh.exe")
        if shell is None:
            shell = (
                Path(os.environ.get("WINDIR", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
        environment = {
            **os.environ,
            "PRINTING_AGENT_SIGNATURE_PATH": str(path),
        }
        if Path(shell).name.casefold() == "powershell.exe":
            environment["PSModulePath"] = os.pathsep.join(
                [
                    str(
                        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                        / "WindowsPowerShell"
                        / "Modules"
                    ),
                    str(
                        Path(os.environ.get("WINDIR", r"C:\Windows"))
                        / "System32"
                        / "WindowsPowerShell"
                        / "v1.0"
                        / "Modules"
                    ),
                ]
            )
        result = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        if result.returncode != 0:
            raise ExternalServiceError("Could not verify Bambu Connect signature")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(
                "Bambu Connect signature result is invalid"
            ) from exc
        return {
            "status": payload.get("Status"),
            "subject": payload.get("Subject"),
            "thumbprint": payload.get("Thumbprint"),
            "file_version": payload.get("FileVersion"),
        }

    @classmethod
    def _signature_is_trusted(cls, signature: dict[str, str | None]) -> bool:
        return (
            signature.get("status") == "Valid"
            and cls._EXPECTED_SIGNER in (signature.get("subject") or "")
        )
