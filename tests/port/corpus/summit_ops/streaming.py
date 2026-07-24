"""Ascent-progress streaming: a websocket endpoint + a multiprocessing worker.

Idiom: a `multiprocessing.Process` writes progress to an S3 JSON state file that the
client polls, while the websocket relays it (custom close code on completion).
"""
from __future__ import annotations

import json
import multiprocessing
import time

from fastapi import APIRouter, WebSocket

from summit_s3 import read_state, write_state  # in-house S3 state helpers (anonymized)

ws_router = APIRouter()


def _run_ascent(job_id: str) -> None:
    for pct in range(0, 101, 20):
        write_state(job_id, {"progress": pct, "ts": time.time()})
        time.sleep(1)


@ws_router.websocket("/ascents/{job_id}/progress")
async def ascent_progress(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    proc = multiprocessing.Process(target=_run_ascent, args=(job_id,))
    proc.start()
    try:
        while proc.is_alive():
            state = read_state(job_id)
            await websocket.send_text(json.dumps(state))
            if state.get("progress") == 100:
                break
    finally:
        await websocket.close(code=4001)
        proc.join()
