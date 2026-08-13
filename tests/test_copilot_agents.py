from __future__ import annotations

from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.config import Settings
from printing_agent.copilot_agents import CopilotDiscoveryAgent, _DiscoveryToolState
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
