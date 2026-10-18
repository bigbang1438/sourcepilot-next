"""Agent protocol fake covers feedback, evidence checks and durable graph resume."""

import json
import time

from fastapi.testclient import TestClient

from sourcepilot.api import create_app
from tests.test_documents import WRITE, OfflineModels


def wait(c, run_id):
    for _ in range(300):
        run = c.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.01)
    raise AssertionError("task did not settle")


class FeedbackModels(OfflineModels):
    def decide(self, messages, timeout=90):
        observations = [
            json.loads(m["content"])
            for m in messages
            if m["role"] == "user" and m["content"].startswith('{"observation"')
        ]
        if not observations:
            return {"tool": "search", "args": {"query": "完全无关词xyz"}}, 8
        last = observations[-1]["observation"]
        if last.get("hits") == []:
            return {"tool": "search", "args": {"query": "保留"}}, 8
        if last.get("hits"):
            hit = last["hits"][0]
            return {
                "tool": "read_page",
                "args": {"documentId": hit["documentId"], "page": hit["page"]},
            }, 8
        page = last
        return {
            "tool": "finish",
            "args": {
                "answer": "资料说明保留 7 天。",
                "citations": [{"documentId": page["documentId"], "page": 1, "quote": "保留 7 天"}],
            },
        }, 8


def test_agent_revises_empty_search_reads_and_cites_source(tmp_path):
    with TestClient(create_app(tmp_path, FeedbackModels()), base_url="http://localhost") as c:
        c.post(
            "/api/documents",
            files={"file": ("guide.txt", "记录保留 7 天。".encode())},
            headers=WRITE,
        )
        run_id = c.post("/api/runs", json={"question": "记录保留多久？"}, headers=WRITE).json()[
            "id"
        ]
        run = wait(c, run_id)
        assert run["status"] == "completed", run
        assert [s["tool"] for s in run["steps"]] == ["search", "search", "read_page", "finish"]
        assert run["citations"][0]["quote"] == "保留 7 天"
        assert run["citations"][0]["version"] == 1
        before = c.get(run["reportUrl"]).text
        c.post(f"/api/runs/{run_id}/resume", headers=WRITE)
        assert c.get(run["reportUrl"]).text == before
        assert before.count("# 研读报告") == 1


class ForgedModels(OfflineModels):
    def decide(self, messages, timeout=90):
        return {
            "tool": "finish",
            "args": {
                "answer": "保留 99 天",
                "citations": [{"documentId": "made-up", "page": 1, "quote": "保留 99 天"}],
            },
        }, 2


def test_fabricated_citation_never_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCEPILOT_MAX_STEPS", "3")
    with TestClient(create_app(tmp_path, ForgedModels()), base_url="http://localhost") as c:
        run_id = c.post("/api/runs", json={"question": "多久？"}, headers=WRITE).json()["id"]
        run = wait(c, run_id)
        assert run["status"] == "budget_exceeded"
        assert run["citations"] == []
        assert run["answer"] == ""
        assert all(step["status"] == "error" for step in run["steps"])


def test_missing_model_fails_explicitly(tmp_path):
    class MissingModels(OfflineModels):
        def decide(self, messages, timeout=90):
            raise RuntimeError("本机模型连接失败")

    with TestClient(create_app(tmp_path, MissingModels()), base_url="http://localhost") as c:
        run_id = c.post("/api/runs", json={"question": "多久？"}, headers=WRITE).json()["id"]
        run = wait(c, run_id)
        assert run["status"] == "failed"
        assert "模型" in run["error"]
        assert run["answer"] == ""
