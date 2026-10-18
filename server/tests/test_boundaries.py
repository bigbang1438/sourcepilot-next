"""Concrete edge cases: content hashes, bad origins, image types and bounded decisions."""

import io
import json
import subprocess
import sys
import time

from fastapi.testclient import TestClient
from PIL import Image

from sourcepilot.api import create_app
from tests.test_agent import FeedbackModels, wait
from tests.test_documents import WRITE, OfflineModels


def test_duplicate_content_under_another_name_does_not_create_duplicate(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        one = c.post(
            "/api/documents", files={"file": ("one.txt", b"same content")}, headers=WRITE
        ).json()
        two = c.post(
            "/api/documents", files={"file": ("two.txt", b"same content")}, headers=WRITE
        ).json()
        assert two["id"] == one["id"]
        assert len(c.get("/api/documents").json()) == 1


def test_malformed_browser_headers_do_not_crash(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        assert (
            c.get("/api/health", headers={"Origin": "http://localhost:not-a-port"}).status_code
            == 403
        )
        assert c.get("/api/health", headers={"Host": "[broken"}).status_code == 403


def test_mismatched_image_extension_is_rejected(tmp_path):
    output = io.BytesIO()
    Image.new("RGB", (12, 12)).save(output, format="JPEG")

    class VisionModels(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            return "图片文字"

    with TestClient(create_app(tmp_path, VisionModels()), base_url="http://localhost") as c:
        result = c.post(
            "/api/documents",
            files={"file": ("fake.png", output.getvalue(), "image/png")},
            headers=WRITE,
        )
        assert result.status_code == 400


def test_real_numpy_cosine_and_rrf_are_distinct_from_keyword_only(tmp_path):
    class SemanticModels(OfflineModels):
        def embed(self, texts, timeout=90):
            return [[1.0, 0.0] if "automobile" in t or t == "car" else [0.0, 1.0] for t in texts]

    with TestClient(create_app(tmp_path, SemanticModels()), base_url="http://localhost") as c:
        a = c.post(
            "/api/documents",
            files={"file": ("semantic.txt", b"automobile transport")},
            headers=WRITE,
        ).json()
        b = c.post(
            "/api/documents", files={"file": ("lexical.txt", b"car warranty")}, headers=WRITE
        ).json()
        lexical = c.post("/api/search", json={"query": "car", "mode": "bm25"}, headers=WRITE).json()
        vector = c.post(
            "/api/search", json={"query": "car", "mode": "vector"}, headers=WRITE
        ).json()
        hybrid = c.post(
            "/api/search", json={"query": "car", "mode": "hybrid"}, headers=WRITE
        ).json()
        assert lexical["hits"][0]["documentId"] == b["id"]
        assert vector["hits"][0]["documentId"] == a["id"]
        assert hybrid["mode"] == "hybrid"
        assert hybrid["hits"][0]["documentId"] == b["id"]
        assert abs(hybrid["hits"][0]["score"] - (1 / 61 + 1 / 62)) < 1e-10


def test_cancel_then_resume_uses_original_document_snapshot(tmp_path):
    import threading

    arrived = threading.Event()
    release = threading.Event()

    class SlowModels(FeedbackModels):
        def decide(self, messages, timeout=90):
            if not arrived.is_set():
                arrived.set()
                release.wait(3)
            return super().decide(messages, timeout)

    app = create_app(tmp_path, SlowModels())
    with TestClient(app, base_url="http://localhost") as c:
        c.post(
            "/api/documents",
            files={"file": ("guide.txt", "记录保留 7 天。".encode())},
            headers=WRITE,
        )
        run_id = c.post("/api/runs", json={"question": "保留多久"}, headers=WRITE).json()["id"]
        assert arrived.wait(3)
        c.post(
            "/api/documents",
            files={"file": ("guide.txt", "记录保留 30 天。".encode())},
            headers=WRITE,
        )
        assert c.post(f"/api/runs/{run_id}/cancel", headers=WRITE).json()["status"] == "cancelled"
        release.set()
        for _ in range(200):
            if run_id not in app.state.runner.active:
                break
            time.sleep(0.01)
        c.post(f"/api/runs/{run_id}/resume", headers=WRITE)
        result = wait(c, run_id)
        assert result["status"] == "completed", result
        assert result["citations"][0]["version"] == 1
        assert result["citations"][0]["quote"] == "保留 7 天"


def test_checkpoint_recovers_after_an_actual_process_exit(tmp_path):
    # A child process dies only after a search node was durably checkpointed.
    script = """
import os,time
from pathlib import Path
from sourcepilot.api import create_app
from tests.test_agent import FeedbackModels
class CrashModels(FeedbackModels):
    def decide(self,messages,timeout=90):
        if any(m["content"].startswith('{"observation"') for m in messages):
            os._exit(23)
        return super().decide(messages,timeout)
app=create_app(Path(__import__('sys').argv[1]),CrashModels())
app.state.store.ingest("guide.txt","记录保留 7 天。".encode(),CrashModels())
run_id=app.state.store.create_run("保留多久")
(Path(__import__('sys').argv[1])/"run-id").write_text(run_id)
app.state.runner.submit(run_id)
time.sleep(10)
os._exit(99)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], timeout=15, capture_output=True
    )
    assert process.returncode == 23, process.stderr.decode(errors="replace")
    run_id = (tmp_path / "run-id").read_text()
    with TestClient(create_app(tmp_path, FeedbackModels()), base_url="http://localhost") as c:
        assert c.get(f"/api/runs/{run_id}").json()["status"] == "interrupted"
        c.post(f"/api/runs/{run_id}/resume", headers=WRITE)
        result = wait(c, run_id)
        assert result["status"] == "completed", result
        assert [s["tool"] for s in result["steps"]] == ["search", "search", "read_page", "finish"]


def test_figure_tool_uses_visual_observation_and_cannot_access_unselected_docs(tmp_path):
    class FigureModels(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            assert image.startswith(b"\x89PNG")
            return "图表标记 N2，ISO800，误差 15%。"

        def decide(self, messages, timeout=90):
            initial = json.loads(messages[1]["content"])
            doc_id = next(iter(initial["documents"]))
            if len(messages) == 2:
                return {"tool": "inspect_figure", "args": {"documentId": doc_id, "page": 1}}, 3
            return {
                "tool": "finish",
                "args": {
                    "answer": "图中误差为 15%。",
                    "citations": [{"documentId": doc_id, "page": 1, "quote": "误差 15%"}],
                },
            }, 3

    output = io.BytesIO()
    Image.new("RGB", (12, 12)).save(output, format="PNG")
    with TestClient(create_app(tmp_path, FigureModels()), base_url="http://localhost") as c:
        doc = c.post(
            "/api/documents",
            files={"file": ("figure.png", output.getvalue(), "image/png")},
            headers=WRITE,
        ).json()
        page = c.get(f"/api/documents/{doc['id']}/pages/1").json()
        assert c.get(page["imageUrl"]).content.startswith(b"\x89PNG")
        run_id = c.post(
            "/api/runs", json={"question": "图中误差？", "documentIds": [doc["id"]]}, headers=WRITE
        ).json()["id"]
        result = wait(c, run_id)
        assert result["status"] == "completed", result
        assert result["steps"][0]["tool"] == "inspect_figure"
        assert result["citations"][0]["quote"] == "误差 15%"
