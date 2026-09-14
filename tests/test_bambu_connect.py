from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import sha256_file
from printing_agent.bambu_connect import (
    BambuConnectManager,
    BambuConnectReadiness,
)
from printing_agent.cloud_inventory import (
    CloudPrintStatusObservation,
    DeviceSummary,
)
from printing_agent.config import Settings
from printing_agent.errors import PolicyViolationError
from printing_agent.fabrication import (
    BambuConnectHandoff,
    BambuConnectSetupConfirmation,
    SlicedArtifact,
)
from printing_agent.fabrication_drivers import BambuStudioCliDriver
from printing_agent.fabrication_profiles import built_in_h2d_profile
from printing_agent.repositories import WorkflowRepository


def _sliced(path: Path) -> SlicedArtifact:
    return SlicedArtifact(
        slice_job_id="slice-job",
        workflow_id="workflow",
        path=str(path),
        digest=sha256_file(path),
        size_bytes=path.stat().st_size,
        format="gcode.3mf",
        model_manifest_digest="a" * 64,
        printer_snapshot_digest="b" * 64,
        cloud_snapshot_digest="c" * 64,
        material_assignment_digest="d" * 64,
        slicer_driver_id="bambu_studio_cli",
        slicer_version="test",
        machine_profile_id="H2D",
        process_profile_id="standard",
        plate_count=1,
        manifest_digest="e" * 64,
    )


def test_connect_handoff_stages_exact_bytes_and_builds_fixed_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slice_dir = tmp_path / "slices"
    source = slice_dir / "workflow" / "slice-job" / "result.gcode.3mf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified gcode 3mf")
    manager = BambuConnectManager(
        Settings(
            data_dir=tmp_path / "data",
            database_url=tmp_path / "data" / "agent.db",
            artifact_dir=tmp_path / "artifacts",
            candidate_cache_dir=tmp_path / "cache",
            simulator_spool_dir=tmp_path / "simulator",
            slice_dir=slice_dir,
        )
    )
    monkeypatch.setattr(
        manager,
        "readiness",
        lambda: BambuConnectReadiness(
            installed=True,
            scheme_registered=True,
            signature_valid=True,
            ready=True,
            message="ready",
        ),
    )
    monkeypatch.setattr(
        BambuStudioCliDriver,
        "_validate_gcode_3mf",
        staticmethod(lambda _path: None),
    )
    monkeypatch.setattr(
        BambuStudioCliDriver,
        "_validate_bambu_connect_compatibility",
        staticmethod(lambda _path: None),
    )

    handoff = manager.prepare_handoff(
        workflow_id="workflow",
        sliced=_sliced(source),
        expected_device_ref="f" * 64,
        expected_device_name="Workshop H2D",
        attempt=1,
        baseline_state="idle",
    )

    staged = Path(handoff.staged_path)
    assert staged.read_bytes() == source.read_bytes()
    assert sha256_file(staged) == handoff.sliced_artifact_digest
    assert handoff.launch_uri.startswith("bambu-connect://import-file?")
    assert handoff.correlation_name in handoff.launch_uri
    second = manager.prepare_handoff(
        workflow_id="workflow",
        sliced=_sliced(source).model_copy(update={"slice_job_id": "slice-job-2"}),
        expected_device_ref="f" * 64,
        expected_device_name="Workshop H2D",
        attempt=1,
        baseline_state="idle",
    )
    assert Path(second.staged_path).parent != staged.parent
    monkeypatch.setattr("os.startfile", lambda _uri: None)
    manager.launch(handoff)
    with pytest.raises(PolicyViolationError, match="verified artifact"):
        manager.launch(
            handoff.model_copy(
                update={
                    "launch_uri": (
                        "bambu-connect://import-file?"
                        "path=C%3A%5CWindows%5Cwin.ini&"
                        f"name={handoff.correlation_name}&version=1.0.0"
                    )
                }
            )
        )


def test_connect_download_url_is_strictly_allowlisted() -> None:
    with pytest.raises(PolicyViolationError, match="not trusted"):
        BambuConnectManager._validate_download_url(
            "https://example.com/bambu-connect.exe"
        )
    with pytest.raises(PolicyViolationError, match="not trusted"):
        BambuConnectManager._validate_download_url(
            "http://public-cdn.bblmw.com/bambu-connect.exe"
        )


def test_connect_status_requires_expected_filename_or_task_name() -> None:
    handoff = BambuConnectHandoff(
        workflow_id="workflow",
        slice_job_id="slice",
        sliced_artifact_digest="a" * 64,
        sliced_manifest_digest="b" * 64,
        expected_device_ref="c" * 64,
        expected_device_name="Workshop H2D",
        staged_path="C:\\slices\\agent.gcode.3mf",
        correlation_name="agent-workflow-deadbeef-a1",
        launch_uri="bambu-connect://import-file?path=verified",
    )
    device = DeviceSummary(
        device_id="H2D-SERIAL",
        name="Workshop H2D",
        model="H2D",
        online=True,
    )
    matching = CloudPrintStatusObservation(
        region="china",
        device=device,
        state="printing",
        raw_state="RUNNING",
        gcode_file="/model/agent-workflow-deadbeef-a1.gcode.3mf",
        subtask_name=None,
        task_id="task-1",
        progress_percent=1,
        remaining_time_seconds=600,
        observed_at=datetime.now(UTC),
    )
    unrelated = matching.model_copy(
        update={"gcode_file": "/model/unrelated.gcode.3mf"}
    )
    prefixed = matching.model_copy(
        update={
            "gcode_file": (
                "/model/prefix-agent-workflow-deadbeef-a1-suffix.gcode.3mf"
            )
        }
    )

    assert PrintingApplication._connect_status_matches(handoff, matching)
    assert not PrintingApplication._connect_status_matches(handoff, unrelated)
    assert not PrintingApplication._connect_status_matches(handoff, prefixed)


async def test_connect_setup_confirmation_is_bound_to_install_and_device(
    repository: WorkflowRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = built_in_h2d_profile()
    profile = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "cloud_device_name": "Workshop H2D",
                    "cloud_device_serial": "H2D-PRIVATE-SERIAL",
                }
            ),
            "digest": None,
        }
    ).with_digest()
    await repository.save_printer_profile(profile)
    readiness = BambuConnectReadiness(
        installed=True,
        scheme_registered=True,
        signature_valid=True,
        ready=True,
        message="ready",
        installation_digest="a" * 64,
        signer_thumbprint="thumbprint",
        file_version="2.5.0",
    )
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.bambu_connect = BambuConnectManager(
        Settings(database_url=repository.database_path)
    )
    monkeypatch.setattr(
        application.bambu_connect,
        "readiness",
        lambda: readiness,
    )
    device_ref = application._device_ref("H2D-PRIVATE-SERIAL")

    confirmation = await application.confirm_bambu_connect_setup(
        profile.profile_id,
        profile_revision=profile.revision,
        expected_device_ref=device_ref,
        expected_installation_digest=readiness.installation_digest or "",
        confirmed_by="test",
    )
    assert isinstance(confirmation, BambuConnectSetupConfirmation)
    assert (
        await application.bambu_connect_setup_status(profile.profile_id)
    )["active"] is True

    monkeypatch.setattr(
        application.bambu_connect,
        "readiness",
        lambda: readiness.model_copy(
            update={"installation_digest": "b" * 64}
        ),
    )
    stale = await application.bambu_connect_setup_status(profile.profile_id)
    assert stale["active"] is False
    assert stale["status"] == "stale"
