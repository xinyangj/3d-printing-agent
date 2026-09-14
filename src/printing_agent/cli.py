from __future__ import annotations

import argparse
import asyncio
import json

from printing_agent.api import main as serve
from printing_agent.bootstrap import build_container


async def _run(args: argparse.Namespace) -> None:
    container = await build_container()
    try:
        if args.command == "create":
            workflow = await container.application.create_workflow(
                args.requirement,
                args.printer,
            )
            print(workflow.model_dump_json(indent=2))
        elif args.command == "get":
            workflow = await container.repository.get_workflow(args.workflow_id)
            print(workflow.model_dump_json(indent=2))
        elif args.command == "approve":
            await container.application.approve(
                args.workflow_id,
                args.version,
                args.digest,
                args.approved_by,
            )
            print(json.dumps({"status": "approved"}))
        elif args.command == "print":
            await container.application.request_print(args.workflow_id)
            print(json.dumps({"status": "print_queued"}))
    finally:
        await container.catalog.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="printing-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    create = subparsers.add_parser("create")
    create.add_argument("requirement")
    create.add_argument("--printer", default="simulator")
    get = subparsers.add_parser("get")
    get.add_argument("workflow_id")
    approve = subparsers.add_parser("approve")
    approve.add_argument("workflow_id")
    approve.add_argument("version", type=int)
    approve.add_argument("digest")
    approve.add_argument("--approved-by", default="local-cli")
    submit_print = subparsers.add_parser("print")
    submit_print.add_argument("workflow_id")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    else:
        asyncio.run(_run(args))
