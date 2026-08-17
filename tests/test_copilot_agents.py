from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.generated.session_events import PermissionRequest
from copilot.tools import ToolInvocation

from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.config import Settings
from printing_agent.copilot_agents import (
    CopilotDiscoveryAgent,
    CopilotModelingAgent,
    _DiscoveryToolState,
    _role_permission_handler,
)
from printing_agent.domain import (
    AnnotationOrigin,
    ArtifactProvenance,
    CandidateFile,
    CandidatePageInspection,
    Dimensions,
    MeshReport,
    ModelArtifact,
    ModelCandidate,
    ModelDecision,
    ModelingHandoff,
    ModelPlan,
    PartDefinition,
    PartGeometryKind,
    PartInstance,
    PartProject,
    PrinterCapabilitySummary,
)
from printing_agent.repositories import WorkflowRepository


async def test_discovery_tools_match_installed_sdk(
    settings: Settings,
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Create a cable clip", "simulator")
    catalog = ThingiverseCatalog(settings)
    try:
        agent = CopilotDiscoveryAgent(settings, repository, catalog)
        tools = agent._build_tools(_DiscoveryToolState(workflow), allow_plan=True)
    finally:
        await catalog.close()

    assert [tool.name for tool in tools] == [
        "submit_model_plan",
        "search_model_catalog",
        "inspect_model_candidate",
        "select_model_candidate",
    ]
    assert all(tool.defer == "never" for tool in tools)


async def test_role_permission_handler_uses_sdk_decision_types() -> None:
    handler = _role_permission_handler({"search_model_catalog"})

    approved = await handler(
        cast(PermissionRequest, SimpleNamespace(tool_name="search_model_catalog")),
        {},
    )
    rejected = await handler(
        cast(PermissionRequest, SimpleNamespace(tool_name="shell")),
        {},
    )

    assert isinstance(approved, PermissionDecisionApproveOnce)
    assert isinstance(rejected, PermissionDecisionReject)


async def test_rejected_candidate_cannot_be_selected_again(
    settings: Settings,
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Create a car", "simulator")
    candidate = ModelCandidate(
        id="rejected-car",
        title="Rejected car",
        source_url="https://example.test/car",
        creator="maker",
        license="CC BY",
        files=[CandidateFile(id="car-file", name="car.stl", format="stl")],
    )
    await repository.save_page_inspection(
        CandidatePageInspection(
            workflow_id=workflow.id,
            search_round_id="round",
            candidate=candidate,
        )
    )
    catalog = ThingiverseCatalog(settings)
    try:
        agent = CopilotDiscoveryAgent(settings, repository, catalog)
        state = _DiscoveryToolState(
            workflow=workflow,
            plan=ModelPlan(search_query="car", geometry_summary="A toy car"),
            rejected_candidate_ids={candidate.id},
        )
        tool = next(
            item
            for item in agent._build_tools(state, allow_plan=False)
            if item.name == "select_model_candidate"
        )
        assert tool.handler is not None
        result = await tool.handler(
            ToolInvocation(
                tool_name=tool.name,
                arguments={
                    "decision": ModelDecision.USE_AS_IS.value,
                    "candidate_id": candidate.id,
                    "file_id": "car-file",
                    "rationale": "Try the same candidate again",
                },
            )
        )
    finally:
        await catalog.close()

    assert result.result_type == "rejected"
    assert "excluded" in result.text_result_for_llm


class CaptureRuntime:
    def __init__(self) -> None:
        self.prompt = ""
        self.resume = True
        self.tools: list[Any] = []

    async def run(self, *args: object, **kwargs: object) -> str:
        self.prompt = cast(str, args[3])
        self.resume = cast(bool, kwargs["resume"])
        self.tools = cast(list[Any], args[2])
        raise RuntimeError("captured")


async def test_revision_sessions_are_fresh_and_source_backed(
    settings: Settings,
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    workflow = await repository.create_workflow("Create a cable clip", "simulator")
    plan = ModelPlan(search_query="cable clip", geometry_summary="A rounded cable clip")
    await repository.save_plan(workflow.id, plan)
    printer = PrinterCapabilitySummary(
        name="simulator",
        build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
        accepted_formats={"stl"},
    )
    previous = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=plan,
        decision=ModelDecision.CREATE,
        target_printer=printer,
        discovery_rationale="Create a parametric model",
    ).with_digest()
    revised = previous.model_copy(update={"version": 2, "digest": None}).with_digest()
    source = "difference() { cube([20, 10, 5]); cylinder(h=5, r=2); }"
    source_path = tmp_path / "source.scad"
    model_path = tmp_path / "model.stl"
    source_path.write_text(source, encoding="utf-8")
    model_path.write_bytes(b"solid fixture\nendsolid fixture\n")
    artifact = ModelArtifact(
        workflow_id=workflow.id,
        version=1,
        source_path=source_path,
        model_path=model_path,
        source_digest="a" * 64,
        model_digest="b" * 64,
        manifest_digest="c" * 64,
        mesh=MeshReport(
            dimensions=Dimensions(width_mm=20, depth_mm=10, height_mm=5),
            triangle_count=12,
            connected_components=1,
            watertight=True,
            volume_mm3=900,
        ),
        provenance=ArtifactProvenance(kind="generated"),
    )
    modeling = CopilotModelingAgent(settings, repository, cast(object, None))
    modeling_runtime = CaptureRuntime()
    modeling.runtime = cast(object, modeling_runtime)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="captured"):
        await modeling.revise(revised, artifact, "Round the top edge", source, previous)

    assert modeling_runtime.resume is False
    assert source in modeling_runtime.prompt
    assert "Previous handoff" in modeling_runtime.prompt
    assert "New handoff" in modeling_runtime.prompt

    multipart_artifact = artifact.model_copy(
        update={
            "project": PartProject(
                parts=[
                    PartDefinition(
                        id="body",
                        name="Bottle body",
                        geometry_kind=PartGeometryKind.IMPORTED_MESH,
                        module_name="body",
                        annotation_origin=AnnotationOrigin.CATALOG_METADATA,
                        confidence=1,
                    ),
                    PartDefinition(
                        id="lid",
                        name="Bottle lid",
                        geometry_kind=PartGeometryKind.IMPORTED_MESH,
                        module_name="lid",
                        annotation_origin=AnnotationOrigin.CATALOG_METADATA,
                        confidence=1,
                    ),
                ],
                instances=[
                    PartInstance(
                        id="body_1",
                        part_id="body",
                        name="Bottle body",
                        annotation_origin=AnnotationOrigin.CATALOG_METADATA,
                        confidence=1,
                    ),
                    PartInstance(
                        id="lid_1",
                        part_id="lid",
                        name="Bottle lid",
                        annotation_origin=AnnotationOrigin.CATALOG_METADATA,
                        confidence=1,
                    ),
                ],
                assembly_status="not_provided",
            ),
            "part_meshes": {"body": artifact.mesh, "lid": artifact.mesh},
        }
    )
    with pytest.raises(RuntimeError, match="captured"):
        await modeling.revise(
            revised,
            multipart_artifact,
            "Scale both parts",
            source,
            previous,
            ["body", "lid"],
        )

    assert [tool.name for tool in modeling_runtime.tools] == [
        "submit_multipart_revision"
    ]
    assert "Allowed edit scope: ['body', 'lid']" in modeling_runtime.prompt
    assert "never merge separate part geometry" in modeling_runtime.prompt

    catalog = ThingiverseCatalog(settings)
    try:
        discovery = CopilotDiscoveryAgent(settings, repository, catalog)
        discovery_runtime = CaptureRuntime()
        discovery.runtime = cast(object, discovery_runtime)  # type: ignore[assignment]
        await repository.patch_workflow(workflow.id, discovery_session_id="old-session")
        with pytest.raises(RuntimeError, match="captured"):
            await discovery.restart_with_feedback(workflow.id, "Use a different shape")
    finally:
        await catalog.close()

    assert discovery_runtime.resume is False
    assert workflow.requirement in discovery_runtime.prompt
    assert "Use a different shape" in discovery_runtime.prompt
    assert (await repository.get_workflow(workflow.id)).discovery_session_id is None
