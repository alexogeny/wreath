"""Llama trek schedule compute — an argparse CLI run as an AWS Batch job.

Idiom: a non-web Python entrypoint (no FastAPI) that submits/consumes AWS Batch jobs
and fans out with multiprocessing.
"""
from __future__ import annotations

import argparse
import concurrent.futures

import boto3

from .compute import run_plan
from .storage import extract_artifact


def submit(queue: str, plan_ids: list[str]) -> None:
    batch = boto3.client("batch")
    for plan_id in plan_ids:
        batch.submit_job(
            jobName=f"transition-{plan_id}",
            jobQueue=queue,
            jobDefinition="summit-transition-plan",
            parameters={"planId": plan_id},
        )


def process(plan_id: str) -> str:
    extract_artifact("plans", plan_id, {"plan": run_plan(plan_id)})
    return plan_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run llama transition plans")
    parser.add_argument("plan_ids", nargs="+")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(process, args.plan_ids))


if __name__ == "__main__":
    main()
