"""Failures found by independent real-document tasks, not frozen-answer fixtures."""

import io
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sourcepilot.api import create_app
from sourcepilot.config import Settings
from sourcepilot.models import OllamaModels
from tests.test_agent import wait
from tests.test_documents import WRITE, OfflineModels


class HandleModels(OfflineModels):
    def decide(self, messages, timeout=90):
        observations = [
            json.loads(message["content"])["observation"]
            for message in messages
            if message["content"].startswith('{"observation"')
        ]
        if not observations:
            return {"tool": "search", "args": {"query": "保留"}}, 2
        last = observations[-1]
        if "hits" in last:
            hit = last["hits"][0]
            return {"tool": "read_page", "args": {"pageRef": hit.get("pageRef", "p1")}}, 2
        if last.get("evidence"):
            return {
                "tool": "finish",
                "args": {"answer": "记录保留 7 天。", "evidence": [last["evidence"][0]["id"]]},
            }, 2
        return {"tool": "finish", "args": {"answer": "未找到可引用证据。"}}, 2


def test_short_handles_fill_exact_snapshot_citation_without_model_copying_uuid(tmp_path):
    with TestClient(create_app(tmp_path, HandleModels()), base_url="http://localhost") as client:
        doc = client.post(
            "/api/documents",
            files={"file": ("guide.txt", "记录保留 7 天。".encode())},
            headers=WRITE,
        ).json()
        run_id = client.post(
            "/api/runs", json={"question": "记录保留多久？"}, headers=WRITE
        ).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "completed", result
        assert [step["tool"] for step in result["steps"]] == ["search", "read_page", "finish"]
        assert result["citations"] == [
            {
                "documentId": doc["id"],
                "name": "guide.txt",
                "version": 1,
                "page": 1,
                "quote": "记录保留 7 天。",
                "sourceType": "document_text",
                "evidenceId": "e1",
            }
        ]


def test_unknown_evidence_handle_cannot_become_a_valid_citation(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCEPILOT_MAX_STEPS", "2")

    class Forged(OfflineModels):
        def decide(self, messages, timeout=90):
            return {"tool": "finish", "args": {"answer": "保留 999 天", "evidence": ["e999"]}}, 1

    with TestClient(create_app(tmp_path, Forged()), base_url="http://localhost") as client:
        run_id = client.post("/api/runs", json={"question": "多久？"}, headers=WRITE).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "budget_exceeded"
        assert result["citations"] == []
        assert all("证据标识" in step["summary"] for step in result["steps"])


def test_repeated_read_does_not_repeat_page_body_and_keeps_observed_handles(tmp_path):
    class Repeating(HandleModels):
        def decide(self, messages, timeout=90):
            observations = [
                json.loads(m["content"])["observation"]
                for m in messages
                if m["content"].startswith('{"observation"')
            ]
            if len(observations) == 2:
                return {"tool": "read_page", "args": {"pageRef": "p1"}}, 1
            if len(observations) == 3:
                assert observations[-1]["alreadyRead"] is True
                assert "text" not in observations[-1]
                return {
                    "tool": "finish",
                    "args": {"answer": "记录保留 7 天。", "evidence": ["e1"]},
                }, 1
            return super().decide(messages, timeout)

    with TestClient(create_app(tmp_path, Repeating()), base_url="http://localhost") as client:
        client.post(
            "/api/documents", files={"file": ("a.txt", "记录保留 7 天。".encode())}, headers=WRITE
        )
        run_id = client.post("/api/runs", json={"question": "多久？"}, headers=WRITE).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "completed", result
        assert result["citations"][0]["quote"] == "记录保留 7 天。"


def test_scan_transcription_is_labelled_model_visual_not_document_original(tmp_path):
    class Visual(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            return "图示标注 X8。"

    output = io.BytesIO()
    Image.new("RGB", (12, 12)).save(output, "PNG")
    with TestClient(create_app(tmp_path, Visual()), base_url="http://localhost") as client:
        doc = client.post(
            "/api/documents", files={"file": ("scan.png", output.getvalue())}, headers=WRITE
        ).json()
        page = client.get(f"/api/documents/{doc['id']}/pages/1").json()
        assert page["sourceType"] == "model_visual"


def test_searching_an_already_read_page_keeps_read_cache_and_original_evidence(tmp_path):
    class SearchAgain(HandleModels):
        def decide(self, messages, timeout=90):
            observations = [
                json.loads(m["content"])["observation"]
                for m in messages
                if m["content"].startswith('{"observation"')
            ]
            if len(observations) == 2:
                return {"tool": "search", "args": {"query": "保留"}}, 1
            if len(observations) == 3:
                return {"tool": "read_page", "args": {"pageRef": "p1"}}, 1
            if len(observations) == 4:
                assert observations[-1].get("alreadyRead") is True
                assert observations[-1]["evidenceIds"] == ["e1"]
                assert "text" not in observations[-1]
                return {
                    "tool": "finish",
                    "args": {"answer": "记录保留 7 天。", "evidence": ["e1"]},
                }, 1
            return super().decide(messages, timeout)

    with TestClient(create_app(tmp_path, SearchAgain()), base_url="http://localhost") as client:
        client.post(
            "/api/documents",
            files={"file": ("guide.txt", "记录保留 7 天。".encode())},
            headers=WRITE,
        )
        run_id = client.post("/api/runs", json={"question": "保留多久？"}, headers=WRITE).json()[
            "id"
        ]
        result = wait(client, run_id)
        assert result["status"] == "completed", result
        assert result["citations"][0]["quote"] == "记录保留 7 天。"


def test_budget_is_checked_after_model_returns_not_after_next_graph_node(tmp_path, monkeypatch):
    class Clock:
        current = 100.0

        def __call__(self):
            return self.current

    clock = Clock()
    monkeypatch.setenv("SOURCEPILOT_MAX_SECONDS", "5")
    monkeypatch.setattr("sourcepilot.agent.monotonic", clock)

    class Late(OfflineModels):
        def decide(self, messages, timeout=90):
            clock.current += 6
            return {"tool": "finish", "args": {"answer": "未找到证据。"}}, 1

    with TestClient(create_app(tmp_path, Late()), base_url="http://localhost") as client:
        run_id = client.post("/api/runs", json={"question": "多久？"}, headers=WRITE).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "budget_exceeded"
        assert result["usage"]["seconds"] >= 6
        assert result["steps"] == []


def test_cancelled_response_exposes_occupied_slot_until_worker_really_exits(tmp_path):
    arrived, release = threading.Event(), threading.Event()

    class Blocked(OfflineModels):
        def decide(self, messages, timeout=90):
            arrived.set()
            release.wait(3)
            return {"tool": "finish", "args": {"answer": "未找到证据。"}}, 1

    with TestClient(create_app(tmp_path, Blocked()), base_url="http://localhost") as client:
        try:
            first = client.post("/api/runs", json={"question": "一"}, headers=WRITE).json()["id"]
            assert arrived.wait(2)
            cancelled = client.post(f"/api/runs/{first}/cancel", headers=WRITE).json()
            assert cancelled["status"] == "cancelled"  # backwards-compatible terminal intent
            assert cancelled["executionActive"] is True
            assert cancelled["cancellationRequested"] is True
            resumed = client.post(f"/api/runs/{first}/resume", headers=WRITE)
            assert resumed.status_code == 409
            client.post("/api/runs", json={"question": "二"}, headers=WRITE)
            assert (
                client.post("/api/runs", json={"question": "三"}, headers=WRITE).status_code == 429
            )
        finally:
            release.set()
        for _ in range(200):
            result = client.get(f"/api/runs/{first}").json()
            if not result["executionActive"]:
                break
            time.sleep(0.01)
        assert result["executionActive"] is False


def test_repeated_stream_data_cannot_extend_total_model_request_deadline(tmp_path, monkeypatch):
    import asyncio

    async def endlessly_delayed(*args, **kwargs):
        await asyncio.sleep(0.4)
        return httpx.Response(200, json={"message": {"content": '{"tool":"finish","args":{}}'}})

    def delayed_sync(*args, **kwargs):
        time.sleep(0.4)
        return httpx.Response(
            200,
            json={"message": {"content": '{"tool":"finish","args":{}}'}},
            request=httpx.Request("POST", "http://localhost/api/chat"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", endlessly_delayed)
    monkeypatch.setattr(httpx, "post", delayed_sync)
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        OllamaModels(Settings(tmp_path)).decide([], timeout=0.05)
    assert time.monotonic() - started < 0.3
