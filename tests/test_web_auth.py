"""web 鉴权 API 级用例（评审第四梯队 ⑲：补 middleware 行为测试）。

覆盖四种组合：无token写/无token读/有token错header/有token对header。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import web.server as ws

    monkeypatch.delenv("LIHU_WEB_TOKEN", raising=False)
    return fastapi_testclient.TestClient(ws.app)


def test_write_blocked_without_token(client):
    """无 token：POST /api/settings → 403（只读模式，写接口禁用）。"""
    r = client.post("/api/settings", json={"confirm": True, "changes": {"heatmap.enabled": True}})
    assert r.status_code == 403
    assert "只读" in r.json()["detail"]


def test_write_put_delete_blocked_without_token(client):
    """无 token：其他写方法同样 403（不只 POST）。"""
    for method in (client.put, client.patch):
        r = method("/api/settings", json={})
        assert r.status_code == 403
    assert client.delete("/api/settings").status_code == 403   # delete 无 body


def test_get_allowed_without_token(client):
    """无 token：GET 只读放行（看板可用），不得 401/403。"""
    r = client.get("/api/settings")
    assert r.status_code == 200


def test_wrong_token_401(client, monkeypatch):
    """有 token：错误 Bearer → 401。"""
    monkeypatch.setenv("LIHU_WEB_TOKEN", "right-token")
    r = client.post("/api/settings",
                    headers={"Authorization": "Bearer wrong-token"},
                    json={"confirm": True, "changes": {}})
    assert r.status_code == 401


def test_correct_token_passes_auth_layer(client, monkeypatch):
    """有 token：正确 Bearer 通过鉴权层（进入业务校验，空 changes → 400 而非 401）。"""
    monkeypatch.setenv("LIHU_WEB_TOKEN", "right-token")
    r = client.post("/api/settings",
                    headers={"Authorization": "Bearer right-token"},
                    json={"confirm": True, "changes": {}})
    assert r.status_code == 400          # 鉴权已过，业务层拒绝空变更
    assert r.json()["detail"] == "无变更内容"


def test_get_with_token_requires_header(client, monkeypatch):
    """有 token：GET /api/ 也需 Bearer（原设计：401 时前端弹框输 token）。"""
    monkeypatch.setenv("LIHU_WEB_TOKEN", "right-token")
    r = client.get("/api/settings")
    assert r.status_code == 401
