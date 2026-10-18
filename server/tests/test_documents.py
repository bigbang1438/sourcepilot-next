"""Breaks caught: version overwrite, failed upload replacement, insecure browser writes."""

from fastapi.testclient import TestClient

from sourcepilot.api import create_app

WRITE = {"X-Requested-With": "SourcePilot"}


class OfflineModels:
    def ready(self):
        return {"modelReady": False, "visionReady": False, "rerankerReady": False}

    def embed(self, texts, timeout=90):
        raise RuntimeError("embedding unavailable")

    def vision(self, image, prompt, timeout=120):
        raise RuntimeError("vision unavailable")


def test_version_snapshot_survives_replacement_and_failed_upload(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        old = c.post(
            "/api/documents",
            files={"file": ("guide.txt", "旧版：离线保留 7 天。".encode())},
            headers=WRITE,
        )
        assert old.status_code == 200
        doc = old.json()
        newer = c.post(
            "/api/documents",
            files={"file": ("guide.txt", "新版：离线保留 30 天。".encode())},
            headers=WRITE,
        )
        assert newer.json()["version"] == 2
        assert newer.json()["id"] == doc["id"]
        page = c.get(f"/api/documents/{doc['id']}/pages/1?version=1").json()
        assert page["text"] == "旧版：离线保留 7 天。"
        failed = c.post(
            "/api/documents", files={"file": ("guide.txt", b"\xff\xfe\xfa")}, headers=WRITE
        )
        assert failed.status_code == 400
        assert c.get("/api/documents").json()[0]["version"] == 2
        same = c.post(
            "/api/documents",
            files={"file": ("guide.txt", "新版：离线保留 30 天。".encode())},
            headers=WRITE,
        )
        assert same.json()["version"] == 2


def test_bm25_fallback_is_explicit_and_returns_real_source(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        c.post(
            "/api/documents",
            files={"file": ("guide.md", "备份策略每天运行，版本保留三十天。".encode())},
            headers=WRITE,
        )
        result = c.post("/api/search", json={"query": "备份策略"}, headers=WRITE)
        assert result.status_code == 200
        assert result.json()["mode"] == "bm25"
        assert result.json()["reranked"] is False
        assert "备份策略" in result.json()["hits"][0]["text"]


def test_browser_origin_host_write_and_path_guards(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        assert c.post("/api/runs", json={"question": "hello"}).status_code == 403
        assert (
            c.get("/api/documents", headers={"Origin": "https://evil.invalid"}).status_code == 403
        )
        assert c.get("/api/documents", headers={"Host": "evil.invalid"}).status_code in (400, 403)
        assert (
            c.post(
                "/api/documents", files={"file": ("../../escape.txt", b"bad")}, headers=WRITE
            ).status_code
            == 400
        )
        assert (
            c.post("/api/documents", files={"file": ("bad.exe", b"MZ")}, headers=WRITE).status_code
            == 400
        )
        assert c.get("/api/assets/../../outside.txt").status_code == 404


def test_image_requires_actual_vision_and_preserves_old_version(tmp_path):
    with TestClient(create_app(tmp_path, OfflineModels()), base_url="http://localhost") as c:
        response = c.post(
            "/api/documents", files={"file": ("photo.png", b"not-png", "image/png")}, headers=WRITE
        )
        assert response.status_code == 400
        assert c.get("/api/documents").json() == []
