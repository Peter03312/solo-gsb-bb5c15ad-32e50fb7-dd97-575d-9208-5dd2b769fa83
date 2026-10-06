"""离线工作站迁移（导出 / 导入预览 / 确认导入）API 测试。

两个独立的 SQLite 库模拟两台工作站：
- 源站 source：演示 4 片 + 采纳 A-B（v2）+ 撤销（v3），含一条有效与
  一条已撤销关系及全部不可改快照；
- 目标站 target：SEED_DEMO_DATA=0 的空白库，启动时仅有空白基线（v0）。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_fd1, SRC_DB = tempfile.mkstemp(suffix=".db")
os.close(_fd1)
_fd2, DST_DB = tempfile.mkstemp(suffix=".db")
os.close(_fd2)
os.environ["DATABASE_PATH"] = SRC_DB
os.environ["SEED_DEMO_DATA"] = "1"

from app import main  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def src():
    # 显式重置为全新源库：与同进程内其他测试模块（test_gaps 等）的库隔离
    if os.path.exists(SRC_DB):
        os.unlink(SRC_DB)
    main.SEED_DEMO_DATA = True
    main.DB_PATH = SRC_DB
    with TestClient(app) as c:   # 进入 lifespan：按 main.DB_PATH 建库并播演示数据
        # v2：A-B 采纳拼接（共边接触）
        r = c.post("/api/relations", json={
            "fragment_a": 1, "fragment_b": 2,
            "markers_a": [[35, 0], [38, 15], [33, 30]],
            "markers_b": [[1, 0], [4, 15], [-1, 30]],
            "expected_version": 1,
        })
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 2
        rid_ab = r.json()["relation_id"]
        # 远处碎片 E：与 B(#2) 拼接（不重叠），作为导入后仍有效的关系
        r = c.post("/api/fragments", json={
            "name": "陶片E",
            "contour": [[100, 0], [130, 0], [130, 30], [100, 30]],
        })
        assert r.status_code == 201, r.text
        r = c.post("/api/relations", json={
            "fragment_a": 2, "fragment_b": 5,
            "markers_a": [[46, 0], [46, 30]],
            "markers_b": [[100, 0], [100, 30]],
            "expected_version": 3,
        })
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 4
        # v5：撤销 A-B（保留已撤销历史）；B-E 仍有效
        r = c.post(f"/api/relations/{rid_ab}/undo",
                   json={"expected_version": 4})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 5
        yield c


def _export(c):
    r = c.get("/api/transfer/export")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def package(src):
    return _export(src)


def _open_dst():
    """切到空白目标库（不播演示数据），返回其 TestClient。"""
    if os.path.exists(DST_DB):
        os.unlink(DST_DB)
    if main.db is not None:
        main.db.conn.close()
    main.SEED_DEMO_DATA = False
    main.DB_PATH = DST_DB
    main.db = main.DB(DST_DB)
    main.ensure_baseline(main.db)
    return TestClient(app)


def _open_src():
    """切回源站连接（SQLite 文件与数据仍在磁盘上）。"""
    if main.db is not None:
        main.db.conn.close()
    main.SEED_DEMO_DATA = True
    main.DB_PATH = SRC_DB
    main.db = main.DB(SRC_DB)


@pytest.fixture(autouse=True)
def _restore_source_db():
    # 每个用例后把应用重新指回源库，保证源/目标用例互不串扰
    yield
    _open_src()


@pytest.fixture()
def dst():
    return _open_dst()


# ---------------------------------------------------------------- 导出

def test_export_package_shape(package):
    p = package
    assert p["format"] == "pottery-assembly"
    assert p["format_version"] == "1.0"
    assert p["current_version"] == 5
    assert p["current_assembly"]["version"] == 5
    versions = [s["version"] for s in p["snapshots"]]
    assert versions == [1, 2, 3, 4, 5]
    assert [s["source"] for s in p["snapshots"]] == \
        ["baseline", "accept", "create", "accept", "undo"]
    # 末版快照与包内当前装配一致（同一状态导出）
    assert p["snapshots"][-1]["state"]["relations"] == \
        p["current_assembly"]["relations"]
    # 含有效及已撤销关系
    statuses = {r["active"] for r in p["current_assembly"]["relations"]}
    assert statuses == {True, False}
    undone = next(r for r in p["current_assembly"]["relations"] if not r["active"])
    assert undone["undone_at"]


def test_export_is_read_only(src, package):
    before = src.get("/api/assembly").json()
    versions_before = src.get("/api/assembly/versions").json()
    _export(src)
    after = src.get("/api/assembly").json()
    versions_after = src.get("/api/assembly/versions").json()
    assert after == before
    assert versions_after == versions_before


def test_export_download_header(src):
    r = src.get("/api/transfer/export?download=true")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and ".json" in cd


# ---------------------------------------------------------------- 预览

def test_preview_on_blank_station(package, dst):
    c = dst
    r = c.post("/api/transfer/import/preview", json={"package": package})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["target"]["fragment_count"] == 0
    assert b["target"]["relation_count"] == 0
    assert b["fragment_count"] == 5
    assert b["active_relation_count"] == 1
    assert b["undone_relation_count"] == 1
    assert b["package"]["snapshot_count"] == 5
    assert b["imported_version"] == 5
    assert b["imported_assembly"]["version"] == 5
    # A-B 已撤销：仅 B(#2)-E(#5) 保持一组
    assert b["imported_assembly"]["groups"] == \
        [{"id": 2, "fragment_ids": [2, 5]}]
    # 预览不写库
    assert c.get("/api/assembly").json()["version"] == 0
    assert c.get("/api/fragments").json() == []


def test_preview_does_not_write(package, dst):
    c = dst
    c.post("/api/transfer/import/preview", json={"package": package})
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 0 and asm["fragments"] == []
    vs = c.get("/api/assembly/versions").json()
    assert [x["version"] for x in vs["versions"]] == [0]


# ---------------------------------------------------------------- 成功导入

def test_confirm_import_roundtrip(src, package, dst):
    c = dst
    r = c.post("/api/transfer/import/confirm",
               json={"package": package, "expected_version": 0})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["imported_version"] == 5
    assert b["snapshot_count"] == 5

    asm = c.get("/api/assembly").json()
    src_asm = src.get("/api/assembly").json()
    # 原 ID、轮廓、位姿、标记、状态、时间戳全部保留
    assert asm["fragments"] == src_asm["fragments"]
    assert asm["relations"] == src_asm["relations"]
    assert asm["version"] == 5

    # 快照原版本号 / 来源 / 时间戳全部保留
    vs = c.get("/api/assembly/versions").json()["versions"]
    src_vs = src.get("/api/assembly/versions").json()["versions"]
    assert [(v["version"], v["source"], v["created_at"]) for v in vs] == \
        [(v["version"], v["source"], v["created_at"]) for v in src_vs]

    # 历史查看仍可用
    r = c.get("/api/assembly/versions/2")
    assert r.status_code == 200
    assert r.json()["source"] == "accept"
    assert len(r.json()["assembly"]["fragments"]) == 4

    # 恢复仍可用：恢复到 v2（关系有效、连通组重算）
    r = c.post("/api/assembly/versions/2/restore/preview",
               json={"expected_version": 5})
    assert r.status_code == 200, r.text
    assert r.json()["restored_assembly"]["groups"] == \
        [{"id": 1, "fragment_ids": [1, 2]}]
    r = c.post("/api/assembly/versions/2/restore/confirm",
               json={"expected_version": 5})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 6
    assert c.get("/api/relations").json()[0]["active"] is True

    # 间隙查询仍可在导入的历史快照上使用
    r = c.post("/api/assembly/versions/2/gaps",
               json={"pairs": [{"fragment_a": 1, "fragment_b": 2}]})
    assert r.status_code == 200
    assert r.json()["pairs"][0]["contact"] is True


def test_import_keeps_autoincrement_ids(package, dst):
    c = dst
    r = c.post("/api/transfer/import/confirm",
               json={"package": package, "expected_version": 0})
    assert r.status_code == 201
    r = c.post("/api/fragments", json={
        "name": "陶片F",
        "contour": [[0, 0], [10, 0], [10, 10], [0, 10]],
    })
    assert r.status_code == 201
    # 新碎片 id 接在包内最大 id 5 之后
    assert r.json()["fragment"]["id"] == 6


# ---------------------------------------------------------------- 拒绝场景

def test_unsupported_format_version_rejected(package, dst):
    c = dst
    bad = {**package, "format_version": "9.9"}
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "unsupported_format_version"
    assert any("9.9" in x for x in d["reasons"])
    # 确认端点同样拒绝
    r = c.post("/api/transfer/import/confirm",
               json={"package": bad, "expected_version": 0})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsupported_format_version"


def test_invalid_contour_rejected(package, dst):
    c = dst
    bad = _copy(package)
    # 自交蝴蝶结轮廓
    bad["current_assembly"]["fragments"][0]["contour"] = \
        [[0, 0], [10, 10], [10, 0], [0, 10]]
    _patch_last_snapshot(bad)
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_contour"


def test_relation_reference_missing_rejected(package, dst):
    c = dst
    bad = _copy(package)
    bad["current_assembly"]["relations"].append({
        "id": 99, "fragment_a": 1, "fragment_b": 77,
        "markers_a": [[0, 0], [1, 1]], "markers_b": [[2, 2], [3, 3]],
        "max_error": 0.0, "active": True,
        "created_at": "2026-10-06T00:00:00+00:00", "undone_at": None,
    })
    _patch_last_snapshot(bad)
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "relation_reference_missing"


def test_duplicate_snapshot_version_rejected(package, dst):
    c = dst
    bad = _copy(package)
    bad["snapshots"].append(dict(bad["snapshots"][-1]))
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "duplicate_snapshot_version"


def test_snapshot_gap_rejected(package, dst):
    c = dst
    bad = _copy(package)
    bad["snapshots"] = [s for s in bad["snapshots"] if s["version"] != 2]
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "snapshot_version_gap"


def test_last_snapshot_mismatch_rejected(package, dst):
    c = dst
    bad = _copy(package)
    # 末版快照与当前装配不一致：移动当前装配中一片碎片的位姿
    bad["current_assembly"]["fragments"][0]["x"] = 123.456
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "current_assembly_mismatch"


def test_snapshot_internal_bad_reference_rejected(package, dst):
    c = dst
    bad = _copy(package)
    # 中间快照内部引用缺失碎片（当前装配不动，专门校验快照）
    bad["snapshots"][0]["state"]["relations"].append({
        "id": 88, "fragment_a": 1, "fragment_b": 99,
        "markers_a": [[0, 0], [1, 1]], "markers_b": [[2, 2], [3, 3]],
        "max_error": 0.0, "active": True,
        "created_at": "2026-10-06T00:00:00+00:00", "undone_at": None,
    })
    r = c.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "relation_reference_missing"


def test_rejected_package_leaves_db_untouched(package, dst):
    c = dst
    bad = _copy(package)
    bad["format_version"] = "9.9"
    r = c.post("/api/transfer/import/confirm",
               json={"package": bad, "expected_version": 0})
    assert r.status_code == 400
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 0 and asm["fragments"] == []
    vs = [v["version"] for v in c.get("/api/assembly/versions").json()["versions"]]
    assert vs == [0]


# ---------------------------------------------------------------- 目标库约束 / 并发

def test_non_blank_target_preview_ok_false(package, dst):
    c = dst
    # 目标库录入一片用户碎片 → 不再是空白工作站
    r = c.post("/api/fragments", json={
        "name": "用户碎片",
        "contour": [[0, 0], [10, 0], [10, 10], [0, 10]],
    })
    assert r.status_code == 201
    r = c.post("/api/transfer/import/preview", json={"package": package})
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is False
    assert b["target"]["fragment_count"] == 1
    assert any("空白" in x for x in b["reasons"])


def test_non_blank_target_confirm_rejected(package, dst):
    c = dst
    c.post("/api/fragments", json={
        "name": "用户碎片",
        "contour": [[0, 0], [10, 0], [10, 10], [0, 10]],
    })
    r = c.post("/api/transfer/import/confirm",
               json={"package": package, "expected_version": 1})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "target_not_blank"
    # 库与历史保持原状（用户碎片仍在，版本仍为 1）
    assert len(c.get("/api/fragments").json()) == 1
    assert c.get("/api/assembly").json()["version"] == 1


def test_stale_expected_version_rejected_409(package, dst):
    c = dst
    r = c.post("/api/transfer/import/confirm",
               json={"package": package, "expected_version": 0})
    assert r.status_code == 201
    # 再往已是导入状态的库导入另一包：目标非空白 + 版本过期，版本过期优先 409
    r = c.post("/api/transfer/import/confirm",
               json={"package": package, "expected_version": 2})
    assert r.status_code == 409
    assert c.get("/api/assembly").json()["version"] == 5


def test_malformed_body_422(dst):
    c = dst
    r = c.post("/api/transfer/import/preview", json={"package": []})
    assert r.status_code == 422
    r = c.post("/api/transfer/import/confirm", json={"package": {}})
    assert r.status_code == 422


# ---------------------------------------------------------------- 工具

def _copy(pkg):
    import copy
    return copy.deepcopy(pkg)


def _patch_last_snapshot(bad):
    """把篡改后的当前装配同步到末版快照（专门制造非末版一致性问题时不用）。"""
    last = max(s["version"] for s in bad["snapshots"])
    for s in bad["snapshots"]:
        if s["version"] == last:
            s["state"] = {
                "version": last,
                "fragments": bad["current_assembly"]["fragments"],
                "groups": [],
                "relations": bad["current_assembly"]["relations"],
            }
