"""离线工作站装配迁移 API 测试。

源工作站（SEED_DEMO_DATA=1）构造多版本历史：
- v1 baseline：4 片演示陶片；
- v2 accept：#1-#2 采纳拼接（有效关系，#2 移到 (34,0,0)）；
- v3 undo：撤销 #1-#2（关系保留、active=false、undone_at 有值）。

随后导出迁移包，在另一台**空白工作站**（SEED_DEMO_DATA=0，仅 v0 空白基线）
上预览/确认导入，校验原 ID、轮廓、位姿、标记、状态、时间戳与各快照原版
本号保留，导入后历史查看/恢复/继续拼接/删除可用；再逐类验证整包拒绝与
过期版本拒绝时库与历史保持原状。
"""
import copy
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import app.main as main


def _open_db(path: str, seed: str):
    """切换 main 的全局 DB（DB_PATH 是模块级常量，须显式改它再重建 db）。"""
    os.environ["DATABASE_PATH"] = path
    os.environ["SEED_DEMO_DATA"] = seed
    main.DB_PATH = path
    main.SEED_DEMO_DATA = seed != "0"
    if main.db is not None:
        main.db.conn.close()
    main.db = main.DB(path)
    if main.SEED_DEMO_DATA:
        main.seed_demo(main.db)
    main.ensure_baseline(main.db)


@pytest.fixture(scope="module")
def env():
    src_fd, src_path = tempfile.mkstemp(suffix=".db")
    dst_fd, dst_path = tempfile.mkstemp(suffix=".db")
    os.close(src_fd)
    os.close(dst_fd)

    _open_db(src_path, "1")
    with TestClient(main.app) as src:
        r = src.post("/api/relations", json={
            "fragment_a": 1, "fragment_b": 2,
            "markers_a": [[35, 0], [38, 15], [33, 30]],
            "markers_b": [[1, 0], [4, 15], [-1, 30]],
            "expected_version": 1,
        })
        assert r.status_code == 201 and r.json()["version"] == 2
        r = src.post("/api/relations/1/undo", json={"expected_version": 2})
        assert r.status_code == 200 and r.json()["version"] == 3
        pkg = src.get("/api/transfer/export").json()
        src_versions = src.get("/api/assembly/versions").json()

    _open_db(dst_path, "0")
    with TestClient(main.app) as dst:
        yield {"src": None, "dst": dst, "dst_path": dst_path,
               "pkg": pkg, "src_versions": src_versions}

    main.db.conn.close()
    main.db = None


@pytest.fixture()
def c(env):
    return env["dst"]


def _temp_target(seed: str):
    """生成切换到临时目标库的夹具；用例结束后切回模块目标库（env["dst"]）。"""
    @pytest.fixture()
    def _fixture(env):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _open_db(path, seed)
        with TestClient(main.app) as client:
            yield client
        _open_db(env["dst_path"], "0")
    return _fixture


fresh_blank = _temp_target("0")
seeded_target = _temp_target("1")


# ---------------------------------------------------------------- 导出

def test_export_format_and_contents(env):
    pkg = env["pkg"]
    assert pkg["format"] == {"name": "pottery-assembly-transfer",
                             "version": "1.0"}
    assert pkg["exported_at"]
    asm = pkg["assembly"]
    assert asm["version"] == 3
    assert [f["id"] for f in asm["fragments"]] == [1, 2, 3, 4]
    assert len(asm["relations"]) == 1
    rel = asm["relations"][0]
    assert rel["id"] == 1 and rel["active"] is False
    assert rel["markers_a"] == [[35, 0], [38, 15], [33, 30]]
    assert rel["undone_at"] and rel["created_at"]
    # 全部不可改快照（原版本号/来源/时间戳）
    assert [(s["version"], s["source"]) for s in pkg["snapshots"]] == [
        (1, "baseline"), (2, "accept"), (3, "undo")]
    assert all(s["created_at"] for s in pkg["snapshots"])
    # 末版快照与包内当前装配一致
    assert pkg["snapshots"][-1]["state"]["fragments"] == asm["fragments"]
    assert pkg["snapshots"][-1]["state"]["relations"] == asm["relations"]


def test_export_download_filename(env):
    # 导出端点带 Content-Disposition（浏览器直接下载为版本化文件名）
    r = env["dst"].get("/api/transfer/export")
    assert r.status_code == 200
    assert 'filename="pottery-assembly-v0.json"' in \
        r.headers.get("content-disposition", "")


# ---------------------------------------------------------------- 预览（不写库）

def test_preview_on_blank_target(c, env):
    r = c.post("/api/transfer/import/preview", json={"package": env["pkg"]})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["ok"] is True and pv["format_version"] == "1.0"
    assert pv["target"] == {"current_version": 0, "blank": True,
                            "fragment_count": 0, "relation_count": 0}
    p = pv["package"]
    assert p["current_version"] == 3
    assert p["fragment_count"] == 4
    assert p["relation_count"] == 1
    assert p["active_relation_count"] == 0
    assert p["undone_relation_count"] == 1
    assert p["snapshot_count"] == 3
    assert [v["version"] for v in p["versions"]] == [1, 2, 3]
    assert [x["id"] for x in p["fragments"]] == [1, 2, 3, 4]
    assert p["relations"][0]["active"] is False
    # 导入后装配：位姿沿用（#2 在撤销后回到独立、位姿保持 v2 计算结果）
    a = pv["imported_assembly"]
    assert a["version"] == 3
    assert {f["id"] for f in a["fragments"]} == {1, 2, 3, 4}
    f2 = next(f for f in a["fragments"] if f["id"] == 2)
    assert (f2["x"], f2["y"]) == (34.0, 0.0)
    assert all(g["fragment_ids"] for g in a["groups"]) is True
    assert a["groups"] == []   # 唯一关系已撤销 → 全部独立


def test_preview_is_read_only(c, env):
    before = c.get("/api/assembly").json()
    before_versions = c.get("/api/assembly/versions").json()
    r = c.post("/api/transfer/import/preview", json={"package": env["pkg"]})
    assert r.status_code == 200
    after = c.get("/api/assembly").json()
    after_versions = c.get("/api/assembly/versions").json()
    assert after == before
    assert after_versions == before_versions
    assert len(after_versions["versions"]) == 1  # 仅空白基线


# ---------------------------------------------------------------- 确认导入

def test_confirm_import_preserves_everything(c, env):
    r = c.post("/api/transfer/import/confirm",
               json={"package": env["pkg"], "expected_version": 0})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["imported_version"] == 3
    assert body["snapshot_count"] == 3
    assert body["fragment_count"] == 4
    assert body["relation_count"] == 1

    asm = c.get("/api/assembly").json()
    assert asm["version"] == 3                 # 版本沿用，不 +1
    f1 = next(f for f in asm["fragments"] if f["id"] == 1)
    assert f1["name"] == "陶片A-左半"
    assert f1["contour"][0] == [0, 0]
    f2 = next(f for f in asm["fragments"] if f["id"] == 2)
    assert (f2["x"], f2["y"]) == (34.0, 0.0)
    rel = asm["relations"][0]
    assert rel["id"] == 1
    assert rel["active"] is False
    assert rel["markers_b"] == [[1, 0], [4, 15], [-1, 30]]
    assert rel["undone_at"] == env["pkg"]["assembly"]["relations"][0]["undone_at"]
    assert rel["created_at"] == env["pkg"]["assembly"]["relations"][0]["created_at"]

    # 快照原版本号/来源/原固化时间全部保留，v3 为当前版，未新增快照
    vers = c.get("/api/assembly/versions").json()
    assert vers["current_version"] == 3
    assert [(v["version"], v["source"], v["current"]) for v in vers["versions"]] == [
        (3, "undo", True), (2, "accept", False), (1, "baseline", False)]
    src_snaps = {s["version"]: s for s in env["pkg"]["snapshots"]}
    for v in vers["versions"]:
        assert v["created_at"] == src_snaps[v["version"]]["created_at"]


# ---------------------------------------------------------------- 导入后历史仍可用

def test_history_view_after_import(c, env):
    r = c.get("/api/assembly/versions/2")
    assert r.status_code == 200
    view = r.json()
    assert view["source"] == "accept"
    a = view["assembly"]
    assert a["historical"] is True
    assert a["relations"][0]["active"] is True   # v2 时关系有效
    assert a["groups"] == [{"id": 1, "fragment_ids": [1, 2]}]
    f2 = next(f for f in a["fragments"] if f["id"] == 2)
    assert f2["x"] == 34.0


def test_restore_after_import(c, env):
    r = c.post("/api/assembly/versions/2/restore/preview",
               json={"expected_version": 3})
    assert r.status_code == 200
    assert r.json()["new_version"] == 4
    r = c.post("/api/assembly/versions/2/restore/confirm",
               json={"expected_version": 3})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 4
    asm = c.get("/api/assembly").json()
    assert asm["relations"][0]["active"] is True  # 由撤销→有效
    assert asm["groups"] == [{"id": 1, "fragment_ids": [1, 2]}]
    # 恢复留下新 restore 快照，旧快照仍在
    vers = c.get("/api/assembly/versions").json()
    assert [(v["version"], v["source"]) for v in vers["versions"]] == [
        (4, "restore"), (3, "undo"), (2, "accept"), (1, "baseline")]


def test_continue_joining_after_import(c, env):
    # 撤销恢复出来的有效关系后重新采纳：拼接演算正常，新版本继续递增
    assert c.post("/api/relations/1/undo",
                  json={"expected_version": 4}).status_code == 200
    r = c.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[35, 0], [38, 15], [33, 30]],
        "markers_b": [[1, 0], [4, 15], [-1, 30]],
        "expected_version": 5,
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 6
    assert r.json()["relation_id"] == 2   # 已用 id 不复用


def test_delete_and_recreate_after_import(c, env):
    r = c.delete("/api/fragments/4?expected_version=6")
    assert r.status_code == 200
    r = c.post("/api/fragments",
               json={"name": "陶片F", "contour": [[0, 0], [5, 0], [5, 5], [0, 5]]})
    assert r.status_code == 201
    # AUTOINCREMENT 已回退到导入包内最大 id：新碎片 id 为 5（#4 已删除）
    assert r.json()["fragment"]["id"] == 5
    assert r.json()["version"] == 8


# ---------------------------------------------------------------- 整包拒绝

def _codes(resp):
    return [(e["field"], e["code"]) for e in resp.json()["detail"]["errors"]]


def test_reject_unsupported_format_version(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["format"]["version"] = "9.9"
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "package_rejected"
    assert _codes(r) == [("format.version", "format_version_unsupported")]
    # 立即拒绝：未校验其余内容
    assert fresh_blank.get("/api/assembly").json()["version"] == 0


def test_reject_unknown_format_name(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["format"]["name"] = "something-else"
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert _codes(r)[0][1] == "unsupported_format"


def test_reject_invalid_contour(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["assembly"]["fragments"][0]["contour"] = [[0, 0], [10, 10], [10, 0], [0, 10]]
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    codes = _codes(r)
    assert ("assembly.fragments[0].contour", "invalid_contour") in codes


def test_reject_relation_reference_missing(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["assembly"]["relations"][0]["fragment_b"] = 999
    # 末版快照引用仍完整 → 只报当前装配关系引用缺失 + 末版不一致
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    fields = {code: field for field, code in _codes(r)}
    assert "relation_reference_missing" in fields
    assert "last_snapshot_mismatch" in fields


def test_reject_snapshot_reference_missing(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["snapshots"][1]["state"]["relations"][0]["fragment_a"] = 888
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert any(code == "relation_reference_missing"
               for _f, code in _codes(r))


def test_reject_duplicate_snapshot_versions(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["snapshots"][0]["version"] = 3
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    codes = {code for _f, code in _codes(r)}
    assert "snapshot_version_duplicate" in codes


def test_reject_snapshot_state_version_mismatch(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["snapshots"][0]["state"]["version"] = 77
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert any(code == "snapshot_state_version_mismatch"
               for _f, code in _codes(r))


def test_reject_last_snapshot_content_mismatch(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["assembly"]["fragments"][0]["x"] = 123.456
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert _codes(r)[0][1] == "last_snapshot_mismatch"


def test_reject_last_snapshot_version_mismatch(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["assembly"]["version"] = 99
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert any(code == "last_snapshot_mismatch"
               for _f, code in _codes(r))


def test_reject_empty_snapshots(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["snapshots"] = []
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    assert ("snapshots", "empty_snapshots") in _codes(r)


def test_reject_malformed_fields_collected(fresh_blank, env):
    """畸形位姿/标记/状态/时间戳一次性逐条指出，整包拒绝（不产生 500）。"""
    bad = copy.deepcopy(env["pkg"])
    bad["assembly"]["fragments"][0]["x"] = "not-a-number"
    state = copy.deepcopy(bad["snapshots"][-1]["state"])
    state["fragments"][0]["x"] = "not-a-number"
    state["relations"] = [{
        "id": 9, "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[0, 0]], "markers_b": [["x", 0], [1, 1]],
        "max_error": "bad", "active": "yes",
        "created_at": "", "undone_at": 123,
    }]
    bad["snapshots"][-1]["state"] = state
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": bad})
    assert r.status_code == 400
    codes = {code for _f, code in _codes(r)}
    assert {"invalid_pose", "invalid_markers", "invalid_max_error",
            "invalid_active", "invalid_created_at",
            "invalid_undone_at"} <= codes
    assert fresh_blank.get("/api/assembly").json()["version"] == 0


def test_reject_non_object_package(fresh_blank):
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": [1, 2]})
    # package 字段必须是对象 → pydantic 422
    assert r.status_code == 422


def test_reject_missing_expected_version(fresh_blank, env):
    r = fresh_blank.post("/api/transfer/import/confirm",
                         json={"package": env["pkg"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "expected_version_required"
    assert fresh_blank.get("/api/assembly").json()["version"] == 0


# ---------------------------------------------------------------- 目标库约束

def test_reject_non_blank_target_preview(seeded_target, env):
    r = seeded_target.post("/api/transfer/import/preview",
                           json={"package": env["pkg"]})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "target_not_blank"
    assert d["target"]["blank"] is False
    assert d["target"]["fragment_count"] == 4


def test_reject_non_blank_target_confirm_keeps_db(seeded_target, env):
    r = seeded_target.post("/api/transfer/import/confirm",
                           json={"package": env["pkg"], "expected_version": 1})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "target_not_blank"
    asm = seeded_target.get("/api/assembly").json()
    assert asm["version"] == 1
    assert len(asm["fragments"]) == 4
    # 原历史不变（仍只有演示基线）
    vers = seeded_target.get("/api/assembly/versions").json()
    assert [(v["version"], v["source"]) for v in vers["versions"]] == [
        (1, "baseline")]


def test_stale_expected_version_rejected(fresh_blank, env):
    # 目标当前 v0，携带过期/超前版本 → 409，库与历史保持原状
    r = fresh_blank.post("/api/transfer/import/confirm",
                         json={"package": env["pkg"], "expected_version": 7})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "version_conflict"
    assert d["current_version"] == 0 and d["expected_version"] == 7
    asm = fresh_blank.get("/api/assembly").json()
    assert asm["version"] == 0 and asm["fragments"] == []
    vers = fresh_blank.get("/api/assembly/versions").json()
    assert len(vers["versions"]) == 1 and vers["versions"][0]["source"] == "baseline"


def test_confirm_bad_package_rolls_back(fresh_blank, env):
    bad = copy.deepcopy(env["pkg"])
    bad["format"]["version"] = "2.0"
    r = fresh_blank.post("/api/transfer/import/confirm",
                         json={"package": bad, "expected_version": 0})
    assert r.status_code == 400
    assert fresh_blank.get("/api/assembly").json()["version"] == 0
    assert len(fresh_blank.get("/api/assembly/versions").json()["versions"]) == 1


def test_blank_package_round_trip(fresh_blank):
    """空工作站导出的仅含空白基线的包也可导入（目标基线被替换）。"""
    pkg = fresh_blank.get("/api/transfer/export").json()
    assert pkg["assembly"]["version"] == 0 and len(pkg["snapshots"]) == 1
    r = fresh_blank.post("/api/transfer/import/preview", json={"package": pkg})
    assert r.status_code == 200
    assert r.json()["package"]["snapshot_count"] == 1
    r = fresh_blank.post("/api/transfer/import/confirm",
                         json={"package": pkg, "expected_version": 0})
    assert r.status_code == 201
    assert r.json()["imported_version"] == 0
    vers = fresh_blank.get("/api/assembly/versions").json()
    assert len(vers["versions"]) == 1 and vers["current_version"] == 0
