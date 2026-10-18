"""A tool must pass the remaining run budget to external inference."""

import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from sourcepilot.api import create_app
from tests.test_agent import wait
from tests.test_documents import WRITE, OfflineModels


def test_figure_inference_is_bounded_by_remaining_time_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCEPILOT_MAX_SECONDS", "5")

    class BoundedVision(OfflineModels):
        def vision(self, image, prompt, timeout=120):
            if prompt.startswith("仅根据") and timeout > 5:
                raise RuntimeError("tool did not propagate remaining deadline")
            return "误差 15%。"

        def decide(self, messages, timeout=90):
            doc_id = next(iter(json.loads(messages[1]["content"])["documents"]))
            if len(messages) == 2:
                return {"tool": "inspect_figure", "args": {"documentId": doc_id, "page": 1}}, 1
            return {
                "tool": "finish",
                "args": {
                    "answer": "误差为 15%。",
                    "citations": [{"documentId": doc_id, "page": 1, "quote": "误差 15%"}],
                },
            }, 1

    output = io.BytesIO()
    Image.new("RGB", (10, 10)).save(output, format="PNG")
    with TestClient(create_app(tmp_path, BoundedVision()), base_url="http://localhost") as c:
        c.post(
            "/api/documents",
            files={"file": ("f.png", output.getvalue(), "image/png")},
            headers=WRITE,
        )
        run_id = c.post("/api/runs", json={"question": "误差？"}, headers=WRITE).json()["id"]
        result = wait(c, run_id)
        assert result["status"] == "completed", result
        assert result["steps"][0]["status"] == "success", result
