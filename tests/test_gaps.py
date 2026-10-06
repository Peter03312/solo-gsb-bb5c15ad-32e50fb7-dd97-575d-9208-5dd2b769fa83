"""陶片对间隙查询 API（/api/assembly/versions/{v}/gaps）测试。

数据准备（模块级，只建一次）：
- v1 baseline：4 片演示陶片全部位于原点（A/B/C/D 相互重叠）；
- v2 accept：A(#1)-B(#2) 以 3 对标记拼接，B 移到 (34, 0, 0)，
  A 右缘与 B 左缘共边（接触，间隙 0）；
- v3 create：E(#5) 轮廓 [[100,0],[110,0],[110,10],[100,10]]（远处孤片）。

随后全部用例只读查询，不再变更数据。
"""
import math
import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ["SEED_DEMO_DATA"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def prepared(client):
    """构造 v1→v2→v3 状态，返回各版本号。"""
    r = client.get("/api/assembly")
    assert r.status_code == 200 and r.json()["version"] == 1
    # A-B 采纳拼接：B 平移到 (34,0,0)，与 A 共边
    r = client.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[35, 0], [38, 15], [33, 30]],
        "markers_b": [[1, 0], [4, 15], [-1, 30]],
        "expected_version": 1,
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    # 远处孤片 E
    r = client.post("/api/fragments", json={
        "name": "陶片E",
        "contour": [[100, 0], [110, 0], [110, 10], [100, 10]],
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 3
    return {"client": client, "v_baseline": 1, "v_accept": 2, "v_create": 3}


def _query(client, version, pairs):
    return client.post(f"/api/assembly/versions/{version}/gaps",
                       json={"pairs": pairs})


# ---------------------------------------------------------------- 正常计算

def test_gaps_sorted_by_gap_then_ids(prepared):
    c = prepared["client"]
    r = _query(c, 3, [
        {"fragment_a": 1, "fragment_b": 2},   # 接触 → 0
        {"fragment_a": 2, "fragment_b": 5},   # 间隙 20
        {"fragment_a": 1, "fragment_b": 5},   # 间隙 ≈ 62.201
        {"fragment_a": 1, "fragment_b": 3},   # 重叠 → -1
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == 3
    assert body["current_version"] == 3
    assert body["historical"] is False
    assert body["source"] == "create"
    assert body["pair_count"] == 4
    got = [(p["fragment_a"], p["fragment_b"]) for p in body["pairs"]]
    # 间隙升序：-1（重叠）< 0（接触）< 20 < 62.201
    assert got == [(1, 3), (1, 2), (2, 5), (1, 5)]
    gaps = [p["gap_mm"] for p in body["pairs"]]
    assert gaps == sorted(gaps)


def test_contact_pair(prepared):
    c = prepared["client"]
    r = _query(c, 3, [{"fragment_a": 1, "fragment_b": 2}])
    assert r.status_code == 200, r.text
    p = r.json()["pairs"][0]
    assert p["gap_mm"] == 0.0          # 间隙为零表示接触
    assert p["contact"] is True
    assert p["overlap"] is False
    assert p["overlap_area_mm2"] == 0.0
    # 最近点重合（共边上的某点）
    assert math.hypot(p["point_a"][0] - p["point_b"][0],
                      p["point_a"][1] - p["point_b"][1]) < 1e-6


def test_separated_pair_nearest_points(prepared):
    c = prepared["client"]
    r = _query(c, 3, [{"fragment_a": 2, "fragment_b": 5}])
    assert r.status_code == 200, r.text
    p = r.json()["pairs"][0]
    assert p["gap_mm"] == 20.0         # B 右缘 x=80 与 E 左缘 x=100
    assert p["contact"] is False
    assert p["overlap"] is False
    # 最近点分别位于两片轮廓边界上，间距即间隙
    assert abs(p["point_a"][0] - 80.0) < 1e-3
    assert abs(p["point_b"][0] - 100.0) < 1e-3
    assert abs(p["point_a"][1] - p["point_b"][1]) < 1e-3
    dist = math.hypot(p["point_a"][0] - p["point_b"][0],
                      p["point_a"][1] - p["point_b"][1])
    assert dist == pytest.approx(20.0, abs=1e-3)


def test_diagonal_gap_value(prepared):
    c = prepared["client"]
    r = _query(c, 3, [{"fragment_a": 1, "fragment_b": 5}])
    assert r.status_code == 200, r.text
    p = r.json()["pairs"][0]
    # A 最右顶点 (38,15) 到 E 左缘最近端点 (100,10)：hypot(62, 5)
    assert p["gap_mm"] == pytest.approx(round(math.hypot(62, 5), 4), abs=1e-9)
    dist = math.hypot(p["point_a"][0] - p["point_b"][0],
                      p["point_a"][1] - p["point_b"][1])
    assert dist == pytest.approx(p["gap_mm"], abs=1e-3)


def test_overlap_pair_negative_gap_flag(prepared):
    c = prepared["client"]
    r = _query(c, 3, [{"fragment_a": 1, "fragment_b": 3}])
    assert r.status_code == 200, r.text
    p = r.json()["pairs"][0]
    assert p["overlap"] is True
    assert p["gap_mm"] == -1.0         # 负值仅标识重叠，非可用间隙
    assert p["gap_mm"] < 0
    assert p["contact"] is False
    assert p["overlap_area_mm2"] > 0
    # 重叠时仍给出边界最近点坐标
    assert isinstance(p["point_a"][0], float)
    assert isinstance(p["point_b"][0], float)


def test_same_pair_different_versions(prepared):
    """同一陶片对在不同版本快照下结果不同：计算严格依据所查版本。"""
    c = prepared["client"]
    r1 = _query(c, 1, [{"fragment_a": 1, "fragment_b": 2}])
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["version"] == 1 and b1["historical"] is True
    assert b1["source"] == "baseline"
    p1 = b1["pairs"][0]
    assert p1["overlap"] is True and p1["gap_mm"] == -1.0   # v1 时两片都在原点

    r2 = _query(c, 2, [{"fragment_a": 1, "fragment_b": 2}])
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["version"] == 2 and b2["historical"] is True
    p2 = b2["pairs"][0]
    assert p2["contact"] is True and p2["gap_mm"] == 0.0    # v2 时共边接触


def test_pair_version_matching_request_ok(prepared):
    c = prepared["client"]
    r = _query(c, 3, [{"fragment_a": 1, "fragment_b": 2, "version": 3}])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ---------------------------------------------------------------- 只读语义

def test_query_is_read_only(prepared):
    c = prepared["client"]
    before_asm = c.get("/api/assembly").json()
    before_versions = c.get("/api/assembly/versions").json()
    r = _query(c, 3, [{"fragment_a": 1, "fragment_b": 2},
                      {"fragment_a": 1, "fragment_b": 3}])
    assert r.status_code == 200, r.text
    after_asm = c.get("/api/assembly").json()
    after_versions = c.get("/api/assembly/versions").json()
    # 版本号、位姿、关系、历史快照全部不变
    assert after_asm["version"] == before_asm["version"] == 3
    assert after_asm["fragments"] == before_asm["fragments"]
    assert after_asm["relations"] == before_asm["relations"]
    assert after_versions == before_versions
    assert len(after_versions["versions"]) == 3   # 未产生新快照


# ---------------------------------------------------------------- 整次拒绝

def test_empty_pairs_rejected(prepared):
    r = _query(prepared["client"], 3, [])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "empty_pairs"
    assert d["errors"][0]["field"] == "pairs"


def test_invalid_fragment_id_rejected(prepared):
    r = _query(prepared["client"], 3, [{"fragment_a": 0, "fragment_b": 2}])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_pairs"
    assert d["errors"][0]["code"] == "invalid_fragment_id"
    assert d["errors"][0]["field"] == "pairs[0].fragment_a"


def test_same_fragment_rejected(prepared):
    r = _query(prepared["client"], 3, [{"fragment_a": 1, "fragment_b": 1}])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["errors"][0]["code"] == "same_fragment"
    assert d["errors"][0]["field"] == "pairs[0]"


def test_duplicate_pair_rejected(prepared):
    # (1,2) 与 (2,1) 为同一对（不分先后）
    r = _query(prepared["client"], 3, [{"fragment_a": 1, "fragment_b": 2},
                                       {"fragment_a": 2, "fragment_b": 1}])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["errors"][0]["code"] == "duplicate_pair"
    assert d["errors"][0]["field"] == "pairs[1]"


def test_cross_version_rejected(prepared):
    r = _query(prepared["client"], 3, [{"fragment_a": 1, "fragment_b": 2,
                                        "version": 2}])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["errors"][0]["code"] == "cross_version"
    assert d["errors"][0]["field"] == "pairs[0].version"


def test_multiple_errors_collected(prepared):
    """多个字段问题一次性全部指出，整次拒绝。"""
    r = _query(prepared["client"], 3, [
        {"fragment_a": 0, "fragment_b": 1},    # 无效编号
        {"fragment_a": 2, "fragment_b": 2},    # 同片成对
        {"fragment_a": 1, "fragment_b": 2},    # 有效
        {"fragment_a": 2, "fragment_b": 1},    # 重复
    ])
    assert r.status_code == 400
    d = r.json()["detail"]
    codes = [e["code"] for e in d["errors"]]
    assert codes == ["invalid_fragment_id", "same_fragment", "duplicate_pair"]
    fields = [e["field"] for e in d["errors"]]
    assert fields == ["pairs[0].fragment_a", "pairs[1]", "pairs[3]"]


def test_missing_fragment_rejected(prepared):
    r = _query(prepared["client"], 3, [{"fragment_a": 1, "fragment_b": 99}])
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "fragment_not_found"
    assert d["errors"][0]["field"] == "pairs[0].fragment_b"
    assert "v3" in d["errors"][0]["message"]


def test_version_not_found(prepared):
    r = _query(prepared["client"], 999, [{"fragment_a": 1, "fragment_b": 2}])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "version_not_found"


def test_malformed_body_422(prepared):
    r = _query(prepared["client"], 3, [{"fragment_a": "x", "fragment_b": 2}])
    assert r.status_code == 422
