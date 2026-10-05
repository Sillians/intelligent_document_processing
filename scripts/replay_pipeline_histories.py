"""Replay exported pipeline histories with one worker and an owned executor."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow


async def replay(paths: list[Path], executor: ThreadPoolExecutor) -> dict:
    async def histories():
        for path in paths:
            yield WorkflowHistory.from_json(path.name.removesuffix(".history.json"), path.read_text())

    await Replayer(workflows=[DocumentPipelineWorkflow], workflow_task_executor=executor).replay_workflows(histories())
    return {"passed": True, "histories_replayed": len(paths), "files": [str(path) for path in paths]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"passed": False, "status": "running"}))
    paths = sorted({path for directory in args.directories for path in directory.glob("*.history.json")})
    if not paths:
        parser.error("No exported histories found")
    with ThreadPoolExecutor(max_workers=4) as executor:
        result = asyncio.run(replay(paths, executor))
    if args.report:
        args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
