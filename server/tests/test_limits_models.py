"""External-model failure boundaries and hard agent budget enforcement."""

import io
import struct
import zlib

import fitz
import httpx
import pytest
from fastapi.testclient import TestClient

from sourcepilot.api import create_app
from sourcepilot.config import Settings
from sourcepilot.models import ModelError, OllamaModels
from tests.test_agent import FeedbackModels, wait
from tests.test_documents import WRITE, OfflineModels


def test_truncated_model_output_is_not_accepted_even_when_json_is_valid(tmp_path, monkeypatch):
    async def post(*args, **kwargs):
        return httpx.Response(
            200,
            json={"message": {"content": '{"tool":"finish","args":{}}'}, "done_reason": "length"},
            request=httpx.Request("POST", "http://localhost/api/chat"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ModelError, match="截断"):
        OllamaModels(Settings(tmp_path)).decide([])


def test_token_budget_prevents_tool_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCEPILOT_MAX_TOKENS", "1000")

    class OverBudget(FeedbackModels):
        def decide(self, messages, timeout=90):
            return {"tool": "search", "args": {"query": "保留"}}, 1001

    with TestClient(create_app(tmp_path, OverBudget()), base_url="http://localhost") as c:
        run_id = c.post("/api/runs", json={"question": "保留多久"}, headers=WRITE).json()["id"]
        run = wait(c, run_id)
        assert run["status"] == "budget_exceeded"
        assert "Token" in run["error"]
        assert run["steps"] == []


def test_model_cannot_call_arbitrary_tools_and_receives_validation_feedback(tmp_path):
    class InvalidModels(OfflineModels):
        def decide(self, messages, timeout=90):
            if len(messages) == 2:
                return {"tool": "execute_python", "args": {"code": "print(1)"}}, 3
            if len(messages) == 4:
                return {"tool": "search", "args": {"query": "保留", "path": "C:/secret"}}, 3
            return {
                "tool": "finish",
                "args": {"answer": "未找到可以回答问题的证据。", "citations": []},
            }, 3

    with TestClient(create_app(tmp_path, InvalidModels()), base_url="http://localhost") as c:
        run_id = c.post("/api/runs", json={"question": "保留多久"}, headers=WRITE).json()["id"]
        run = wait(c, run_id)
        assert run["status"] == "completed", run
        assert [step["status"] for step in run["steps"]] == ["error", "error", "success"]
        assert run["citations"] == []


def test_image_only_pdf_uses_actual_raster_observation_and_upload_limits(tmp_path):
    class VisualModels(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            assert image.startswith(b"\x89PNG")
            return "扫描页：编号 N2。"

    with fitz.open() as document:
        document.new_page()
        pdf = document.tobytes()
    with TestClient(create_app(tmp_path, VisualModels()), base_url="http://localhost") as c:
        result = c.post(
            "/api/documents", files={"file": ("scan.pdf", pdf, "application/pdf")}, headers=WRITE
        )
        assert result.status_code == 200
        doc_id = result.json()["id"]
        page = c.get(f"/api/documents/{doc_id}/pages/1").json()
        assert page["text"] == "扫描页：编号 N2。"
        assert c.get(page["imageUrl"]).status_code == 200
        assert (
            c.post(
                "/api/documents",
                content=b"x",
                headers={**WRITE, "Content-Length": str(22 * 1024 * 1024)},
            ).status_code
            == 413
        )


def test_unselected_document_cannot_be_read_by_agent(tmp_path):
    class ScopeModels(OfflineModels):
        target = ""

        def decide(self, messages, timeout=90):
            if len(messages) == 2:
                return {"tool": "read_page", "args": {"documentId": self.target, "page": 1}}, 2
            return {
                "tool": "finish",
                "args": {"answer": "未找到选择范围内的证据。", "citations": []},
            }, 2

    model = ScopeModels()
    with TestClient(create_app(tmp_path, model), base_url="http://localhost") as c:
        doc = c.post(
            "/api/documents", files={"file": ("private.txt", b"secret")}, headers=WRITE
        ).json()
        model.target = doc["id"]
        run_id = c.post(
            "/api/runs", json={"question": "秘密？", "documentIds": []}, headers=WRITE
        ).json()["id"]
        run = wait(c, run_id)
        assert run["status"] == "completed"
        assert run["steps"][0]["status"] == "error"
        assert run["citations"] == []


def test_reranker_true_requires_a_valid_real_model_permutation(tmp_path, monkeypatch):
    models = OllamaModels(Settings(tmp_path, reranker_model="local-reranker"))
    hits = [{"text": "alpha"}, {"text": "beta"}]

    async def post(*args, **kwargs):
        return httpx.Response(
            200,
            json={"message": {"content": '{"order":[1,0]}'}, "done_reason": "stop"},
            request=httpx.Request("POST", "http://localhost/api/chat"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    reordered, reranked = models.rerank("beta", hits)
    assert reranked is True
    assert reordered[0]["text"] == "beta"


@pytest.mark.parametrize("content", ['{"order":[1,"0"]}', '{"order":[false,true]}', "[]"])
def test_malformed_reranker_output_is_explicit_model_error(tmp_path, monkeypatch, content):
    async def post(*args, **kwargs):
        return httpx.Response(
            200,
            json={"message": {"content": content}, "done_reason": "stop"},
            request=httpx.Request("POST", "http://localhost/api/chat"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ModelError):
        OllamaModels(Settings(tmp_path, reranker_model="local-reranker")).rerank(
            "q", [{"text": "a"}, {"text": "b"}]
        )


def test_decompression_bomb_header_is_rejected_without_decoding(tmp_path):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (10, 10)).save(output, format="PNG")
    data = bytearray(output.getvalue())
    data[16:24] = struct.pack(">II", 100000, 100000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        result = c.post(
            "/api/documents", files={"file": ("bomb.png", bytes(data), "image/png")}, headers=WRITE
        )
        assert result.status_code == 400
        assert c.get("/api/documents").json() == []


def test_oversized_pdf_page_is_rejected_before_raster_allocation(tmp_path, monkeypatch):
    def block_allocation(*args, **kwargs):
        raise AssertionError("unsafe raster allocation attempted")

    monkeypatch.setattr(fitz.Page, "get_pixmap", block_allocation)
    with fitz.open() as document:
        document.new_page(width=100000, height=100000)
        pdf = document.tobytes()
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        result = c.post(
            "/api/documents", files={"file": ("huge.pdf", pdf, "application/pdf")}, headers=WRITE
        )
        assert result.status_code == 400
