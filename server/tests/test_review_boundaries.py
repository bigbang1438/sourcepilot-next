"""Reproduce visual-provenance loss and competing terminal transitions."""

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sourcepilot.api import create_app
from tests.test_agent import wait
from tests.test_documents import WRITE, OfflineModels


class EmptyAnswer(OfflineModels):
    def decide(self, messages, timeout=90):
        return {"tool": "finish", "args": {"answer": "未找到可核对的资料。"}}, 1


def wait_released(client, run_id):
    for _ in range(300):
        result = client.get(f"/api/runs/{run_id}").json()
        if not result["executionActive"]:
            return result
        time.sleep(0.01)
    raise AssertionError("application worker did not exit")


def test_search_then_legacy_finish_preserves_scan_visual_provenance(tmp_path):
    class Visual(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            return "扫描说明：记录保留 7 天。"

        def decide(self, messages, timeout=90):
            if len(messages) == 2:
                return {"tool": "search", "args": {"query": "保留"}}, 1
            hit = json.loads(messages[-1]["content"])["observation"]["hits"][0]
            return {
                "tool": "finish",
                "args": {
                    "answer": "扫描说明标记记录保留 7 天，需核对原图。",
                    "citations": [
                        {"documentId": hit["documentId"], "page": hit["page"], "quote": "保留 7 天"}
                    ],
                },
            }, 1

    image = io.BytesIO()
    Image.new("RGB", (12, 12)).save(image, "PNG")
    with TestClient(create_app(tmp_path, Visual()), base_url="http://localhost") as client:
        client.post("/api/documents", files={"file": ("scan.png", image.getvalue())}, headers=WRITE)
        run_id = client.post("/api/runs", json={"question": "保留多久？"}, headers=WRITE).json()[
            "id"
        ]
        result = wait(client, run_id)
        assert result["status"] == "completed", result
        assert [step["tool"] for step in result["steps"]] == ["search", "finish"]
        assert result["citations"][0]["sourceType"] == "model_visual"
        assert "视觉模型解读" in client.get(result["reportUrl"]).text


@pytest.mark.parametrize("stale_cancel_read", [False, True])
def test_completion_commit_and_cancel_response_cannot_overwrite_each_other(
    tmp_path, monkeypatch, stale_cancel_read
):
    app = create_app(tmp_path, EmptyAnswer())
    store = app.state.store
    entered_commit, allow_commit = threading.Event(), threading.Event()
    cancel_read, allow_cancel = threading.Event(), threading.Event()
    original_save, original_run = store.save_report, store.run

    def delayed_save(*args, **kwargs):
        entered_commit.set()
        assert allow_commit.wait(3), "test did not release the completion commit"
        return original_save(*args, **kwargs)

    def observed_run(run_id):
        result = original_run(run_id)
        if stale_cancel_read and result["status"] == "running":
            cancel_read.set()
            assert allow_cancel.wait(3), "test did not release the stale cancellation read"
        return result

    monkeypatch.setattr(store, "save_report", delayed_save)
    monkeypatch.setattr(store, "run", observed_run)
    with (
        TestClient(app, base_url="http://localhost") as client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        run_id = client.post("/api/runs", json={"question": "缺少资料？"}, headers=WRITE).json()[
            "id"
        ]
        assert entered_commit.wait(3)
        cancellation = executor.submit(client.post, f"/api/runs/{run_id}/cancel", headers=WRITE)
        try:
            if stale_cancel_read:
                if cancel_read.wait(0.3):
                    allow_commit.set()
                    for _ in range(200):
                        if store.raw_run(run_id)["status"] == "completed":
                            break
                        time.sleep(0.005)
            else:
                # A correct shared commit lock holds cancellation until commit exits.
                with suppress(TimeoutError):
                    cancellation.result(timeout=0.3)
        finally:
            allow_commit.set()
            allow_cancel.set()
        response = cancellation.result(timeout=3).json()
        result = wait_released(client, run_id)
        assert response["status"] == "completed", response
        assert result["status"] == "completed", result
        assert client.get(f"/api/runs/{run_id}/report").status_code == 200
        assert result["answer"] == "未找到可核对的资料。"


def test_cancellation_winning_before_final_commit_leaves_no_report(tmp_path, monkeypatch):
    app = create_app(tmp_path, EmptyAnswer())
    prepared, release = threading.Event(), threading.Event()
    original_invoke = app.state.runner.graph.invoke

    def pending_commit(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        prepared.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(app.state.runner.graph, "invoke", pending_commit)
    with TestClient(app, base_url="http://localhost") as client:
        run_id = client.post("/api/runs", json={"question": "缺少资料？"}, headers=WRITE).json()[
            "id"
        ]
        assert prepared.wait(3)
        try:
            response = client.post(f"/api/runs/{run_id}/cancel", headers=WRITE).json()
            assert response["status"] == "cancelled"
            assert response["executionActive"] is True
        finally:
            release.set()
        result = wait_released(client, run_id)
        assert result["status"] == "cancelled"
        assert result["answer"] == ""
        assert client.get(f"/api/runs/{run_id}/report").status_code == 404


def test_failed_completion_transaction_cannot_leave_orphan_report(tmp_path):
    app = create_app(tmp_path, EmptyAnswer())
    with app.state.store.connection() as database:
        database.executescript("""
            CREATE TRIGGER reject_completion BEFORE UPDATE OF status ON runs
            WHEN NEW.status='completed'
            BEGIN SELECT RAISE(ABORT, 'simulated commit failure'); END;
        """)
    with TestClient(app, base_url="http://localhost") as client:
        run_id = client.post("/api/runs", json={"question": "缺少资料？"}, headers=WRITE).json()[
            "id"
        ]
        result = wait_released(client, run_id)
        assert result["status"] == "failed", result
        assert result["answer"] == ""
        assert client.get(f"/api/runs/{run_id}/report").status_code == 404
