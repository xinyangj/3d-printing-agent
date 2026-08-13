from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.generated.session_events import PermissionRequest

from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.config import Settings
from printing_agent.copilot_agents import (
    CopilotDiscoveryAgent,
    _DiscoveryToolState,
    _role_permission_handler,
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
