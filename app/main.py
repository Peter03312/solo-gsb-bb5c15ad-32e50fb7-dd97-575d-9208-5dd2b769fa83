"""陶片拼接系统 —— FastAPI 后端。

业务规则：
- 录入毫米制闭合轮廓，自交轮廓整次拒绝；
- 候选拼接需两片各至少 2 处对应边缘标记，标记重合拒绝；
- 采纳拼接仅做平移+旋转：同组对应点误差均 ≤ 1mm，同组轮廓内部不重叠；
- 合并两组时整体移动其中一组，组内相对位姿不变；
- 同组内新增关系（闭环）必须符合已有位姿；
- 任何校验失败整次拒绝，原拼接组不变；
- 撤销关系后按剩余关系重算连通组，孤片恢复独立状态；
- 采纳/撤销携带期望版本号，过期版本返回 409 冲突；
- 有序试拼（/api/trials/rehearse）基于版本快照逐步推演，不落库；
  确认（/api/trials/commit）整体提交，任一步失败或版本过期均不写入，
  成功仅递增一次装配版本（详见 trial.py）。
- 轮廓修订（/api/fragments/{id}/revision/preview|confirm）：提交修订后的
  毫米制闭合轮廓及该片每条有效关系的新边缘标记，以现有位姿校验受影响组
  误差与碰撞；预览不落库，确认时重新校验同一输入并原子更新，位姿、关系
  ID、分组、已撤销历史不变，装配版本只 +1（详见 revision.py）。
- 锚点整体校准（/api/calibration/preview|confirm）：修复师选一片作锚点，
  锚点位姿固定，仅平移+旋转其余碎片，以连通组全部有效关系的成对边缘标记
  共同最小化对应点误差平方和；预览给出调整前后位姿、逐关系误差与轮廓冲突
  且不落库；仅当误差总量严格下降、每对对应点误差 ≤1mm、组内轮廓内部不
  重叠时确认才原子写入位姿与有效关系误差，轮廓/标记/关系状态/分组不变，
  装配版本只递增一次并留不可改快照；版本过期 409，锚点不存在 404、组不足
  两片 400，均不写入（详见 calibration.py）。
- 历史版本（/api/assembly/versions...）：每次成功变更都在同一事务内把
  完整装配（碎片/关系/连通组）固化为不可改版本快照；启用时当前状态记为
  基线。可查看任意历史版的碎片轮廓、位姿、有效及已撤销关系与连通组；
  恢复前先预览恢复后的完整装配及其与当前版的碎片/关系差异（不写库），
  确认须提交预览所依据的当前版本号，版本过期或目标版不存在均拒绝且不
  写入；成功时原子恢复该版碎片与关系的原 ID 及状态、重算连通组，装配
  版本只递增一次并留下新快照，旧快照仍可查（详见 history.py）。
- 陶片对间隙查询（/api/assembly/versions/{v}/gaps）：修复师选择一个装配
  版本与若干陶片对，系统依据该版本快照的固定轮廓与位姿逐对计算轮廓边界
  的最短间隙、对应最近点坐标及是否内部重叠，按间隙从小到大、陶片编号
  排序；只读，不因计算改变位姿、关系或历史，也不产生新快照。缺少陶片、
  重复陶片对、跨版本或无效编号时整次拒绝并指出字段。间隙为 0 表示接触，
  负值（-1）仅标识内部重叠，不得当作可用间隙（详见 gaps.py）。
- 离线工作站迁移（/api/transfer/export、/api/transfer/import/preview|
  confirm）：导出当前装配及全部不可改快照为带格式版本号的 JSON 包
  （只读）；导入先预览包内碎片、有效及已撤销关系、版本数与导入后装配
  （不写库），确认须携带目标库当前装配版本，在单一事务内保留碎片/关系
  原 ID、轮廓、位姿、标记、状态、时间戳及各快照原版本号（导入不新增版本
  与快照）；目标库仅允许无碎片无关系的空白基线，该基线由包整包替换。
  格式版本不支持、轮廓无效、关系引用缺失、快照版本重复/缺号、末版与包内
  当前装配不一致均整包拒绝并列出原因；目标版本过期 409，库与历史原状
  （详见 portable.py、DB.import_package）。
"""
from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Union

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calibration, gaps, geometry, history, portable, revision, trial
from .calibration import CalibrationError, evaluate_calibration
from .database import DB
from .portable import PackageError
from .revision import RevisionError, evaluate_revision
from .trial import TrialError, evaluate_proposal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(BASE_DIR, "..", "data", "app.db"))
SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "1") != "0"

db: DB | None = None


def get_db() -> DB:
    assert db is not None
    return db


# ---------------------------------------------------------------- 请求模型

class FragmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    contour: list[list[float]] = Field(min_length=3)


class PreviewIn(BaseModel):
    fragment_a: int
    fragment_b: int
    markers_a: list[list[float]] = Field(default_factory=list)
    markers_b: list[list[float]] = Field(default_factory=list)


class AcceptIn(PreviewIn):
    expected_version: int


class UndoIn(BaseModel):
    expected_version: int


class TrialRelateStep(BaseModel):
    action: Literal["relate"] = "relate"
    fragment_a: int
    fragment_b: int
    markers_a: list[list[float]] = Field(default_factory=list)
    markers_b: list[list[float]] = Field(default_factory=list)
    # 可选：供序列内后续撤销步引用本步新建的关系
    client_id: str | None = Field(default=None, max_length=100)


class TrialUndoStep(BaseModel):
    action: Literal["undo"] = "undo"
    relation_id: int | None = None       # 撤销已存在的有效关系
    client_id: str | None = None         # 或撤销序列内前序新建关系


TrialStep = Union[TrialRelateStep, TrialUndoStep]


class TrialIn(BaseModel):
    expected_version: int
    steps: list[TrialStep] = Field(default_factory=list)


class RevisionRelationIn(BaseModel):
    relation_id: int
    markers: list[list[float]] = Field(default_factory=list)


class RevisionIn(BaseModel):
    contour: list[list[float]] = Field(min_length=3)
    # 本片每一条有效关系的新边缘标记（遗漏有效关系会被整次拒绝）
    relations: list[RevisionRelationIn] = Field(default_factory=list)
    expected_version: int


class RestoreIn(BaseModel):
    # 预览所依据的当前装配版本；过期则 409 拒绝且不写入
    expected_version: int


class CalibrationIn(BaseModel):
    # 锚点碎片 id（位姿固定）+ 预览所依据的当前装配版本（过期 409）
    anchor_id: int
    expected_version: int


class GapPairIn(BaseModel):
    fragment_a: int
    fragment_b: int
    # 可选：本对所属版本；与路径版本不一致视为跨版本，整次拒绝
    version: int | None = None


class GapsIn(BaseModel):
    # 非空陶片对列表；空列表整次拒绝（400 empty_pairs）
    pairs: list[GapPairIn] = Field(default_factory=list)


class TransferPreviewIn(BaseModel):
    # 迁移包整体（带 format / format_version 的 JSON 对象）
    package: dict


class TransferConfirmIn(TransferPreviewIn):
    # 目标库当前装配版本；过期（他人已变更目标库）→ 409，库与历史保持原状
    expected_version: int


# ---------------------------------------------------------------- 拼接演算

def _load_pair(d: DB, fa_id: int, fb_id: int):
    if fa_id == fb_id:
        raise HTTPException(400, "不能与同一片陶片建立拼接关系")
    fa, fb = d.get_fragment(fa_id), d.get_fragment(fb_id)
    if fa is None or fb is None:
        missing = fa_id if fa is None else fb_id
        raise HTTPException(404, f"碎片 #{missing} 不存在")
    return fa, fb


# 位姿方案演算 evaluate_proposal 位于 trial.py，单条预览/采纳与试拼共用。


def _check_version(d: DB, expected: int):
    current = d.version()
    if expected != current:
        raise HTTPException(
            409, f"版本冲突：当前数据版本为 v{current}，"
                 f"您基于 v{expected} 的操作已过期，请刷新后重试")


# ---------------------------------------------------------------- 应用生命周期

def seed_demo(d: DB):
    """初始化演示数据（仅当库为空且未禁用）。"""
    if d.all_fragments():
        return
    demo = [
        ("陶片A-左半", [[0, 0], [35, 0], [38, 15], [33, 30], [37, 45], [34, 60], [0, 60]]),
        ("陶片B-右半", [[1, 0], [4, 15], [-1, 30], [3, 45], [0, 60], [46, 60], [46, 0]]),
        ("陶片C", [[0, 0], [40, 0], [35, 25], [10, 30]]),
        ("陶片D", [[0, 0], [30, 5], [25, 35], [-5, 20]]),
    ]
    with d.lock:
        for name, contour in demo:
            d.insert_fragment(name, contour)
        d.recompute_groups()
        v = d.bump_version()
        # 演示数据为启用版本历史后的首个状态：记为基线快照
        d.save_snapshot(v, "baseline", d.assembly_state(), ignore=True)
        d.conn.commit()


def ensure_baseline(d: DB):
    """启用版本历史：尚无任何快照时，把当前状态记为基线（不改数据/版本）。

    兼容升级已有数据库：快照表为空时以当前版本补记一条不可改基线快照；
    已存在快照（如演示数据已在 seed_demo 内记录）则保留原快照不动。
    """
    with d.lock:
        if d.has_snapshot():
            return
        v = d.version()
        d.save_snapshot(v, "baseline", d.assembly_state(), ignore=True)
        d.conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = DB(DB_PATH)
    if SEED_DEMO_DATA:
        seed_demo(db)
    ensure_baseline(db)
    yield
    db.conn.close()


app = FastAPI(title="陶片拼接系统", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 碎片 API

@app.post("/api/fragments", status_code=201)
def create_fragment(payload: FragmentIn):
    d = get_db()
    try:
        contour = geometry.validate_contour(payload.contour)
    except geometry.GeometryError as e:
        raise HTTPException(400, str(e))
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            fid = d.insert_fragment(payload.name.strip(), contour)
            v = d.bump_version()
            d.save_snapshot(v, "create", d.assembly_state())
            d.conn.commit()
        except Exception:
            d.conn.rollback()
            raise
        return {"ok": True, "fragment": d.get_fragment(fid),
                "version": v}


@app.get("/api/fragments")
def list_fragments():
    return get_db().all_fragments()


@app.delete("/api/fragments/{fid}")
def delete_fragment(fid: int, expected_version: int):
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, expected_version)
            if d.get_fragment(fid) is None:
                raise HTTPException(404, f"碎片 #{fid} 不存在")
            d.delete_fragment(fid)
            d.recompute_groups()
            v = d.bump_version()
            d.save_snapshot(v, "delete", d.assembly_state())
            d.conn.commit()
        except Exception:
            d.conn.rollback()
            raise
    return {"ok": True, "version": v, "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 轮廓修订 API

def _revision_response(fid: int, expected_version: int, ev: dict) -> dict:
    """把 evaluate_revision 结果整理为预览/确认共用的响应体（不落库语义）。"""
    rel_rows = []
    for row in ev["relations"]:
        rel_rows.append({
            "relation_id": row["relation_id"],
            "revised": row["revised"],
            "side": row["side"],
            "fragment_a": row["fragment_a"],
            "fragment_b": row["fragment_b"],
            "other_fragment_id": row["other_fragment_id"],
            "required_count": row["required_count"],
            "marker_count": row["marker_count"],
            "errors": [round(e, 4) for e in row["errors"]],
            "max_error_mm": (round(row["max_error_mm"], 4)
                             if row["max_error_mm"] is not None else None),
            "conflicts": row["conflicts"],
            "markers_a": row["markers_a"],
            "markers_b": row["markers_b"],
        })
    return {
        "ok": ev["ok"],
        "fragment_id": fid,
        "expected_version": expected_version,
        "independent": ev["independent"],
        "missing_relation_ids": ev["missing_relation_ids"],
        "contour": [list(p) for p in ev["contour"]] if ev["contour"] else None,
        "conflicts": ev["conflicts"],
        "relations": rel_rows,
        "overlaps": [
            {"fragment_a": o["fragment_a"], "fragment_b": o["fragment_b"],
             "name_a": o["name_a"], "name_b": o["name_b"],
             "area_mm2": round(o["area_mm2"], 6)}
            for o in ev["overlaps"]
        ],
        "involved_fragment_ids": ev["involved"],
    }


@app.post("/api/fragments/{fid}/revision/preview")
def preview_revision(fid: int, payload: RevisionIn):
    """预览轮廓修订：逐条误差与冲突原因，不写库。版本过期返回 409。"""
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        if d.get_fragment(fid) is None:
            raise HTTPException(404, f"碎片 #{fid} 不存在")
        rel_inputs = [(r.relation_id, r.markers) for r in payload.relations]
        try:
            ev = evaluate_revision(d, fid, payload.contour, rel_inputs)
        except RevisionError as e:
            raise HTTPException(e.status, e.detail())
        return _revision_response(fid, payload.expected_version, ev)


@app.post("/api/fragments/{fid}/revision/confirm", status_code=201)
def confirm_revision(fid: int, payload: RevisionIn):
    """确认轮廓修订：重新校验同一输入，原子更新轮廓/相关标记/误差。

    位姿、关系 ID、连通分组、已撤销历史保持不变；成功后装配版本只 +1。
    校验失败整次拒绝（不写入任何变化）；版本过期返回 409。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            if d.get_fragment(fid) is None:
                raise HTTPException(404, f"碎片 #{fid} 不存在")
            rel_inputs = [(r.relation_id, r.markers) for r in payload.relations]
            try:
                ev = evaluate_revision(d, fid, payload.contour, rel_inputs)
            except RevisionError as e:
                raise HTTPException(e.status, e.detail())
            if not ev["ok"]:
                raise HTTPException(400, detail={
                    "message": "轮廓修订被拒绝，未写入任何变化",
                    **_revision_response(fid, payload.expected_version, ev),
                })
            d.update_fragment_contour(fid, ev["contour"])
            for rid, up in ev["updates"].items():
                d.update_relation_side(rid, up["side"], up["markers"],
                                       up["max_error"])
            # 不调用 recompute_groups：分组必须保持不变
            new_version = d.bump_version()
            d.save_snapshot(new_version, "revision", d.assembly_state())
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "fragment_id": fid, "version": new_version,
            "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 锚点整体校准 API

def _calibration_response(anchor_id: int, expected_version: int,
                          ev: dict) -> dict:
    """把 evaluate_calibration 结果整理为预览/确认共用的响应体。"""
    def r4(v: float) -> float:
        v = round(v, 4)
        return 0.0 if v == 0 else v   # 归一 -0.0，避免界面显示 -0.00

    return {
        "ok": ev["ok"],
        "anchor_id": anchor_id,
        "expected_version": expected_version,
        "involved_fragment_ids": ev["involved"],
        "sse_before_mm2": round(ev["sse_before"], 6),
        "sse_after_mm2": round(ev["sse_after"], 6),
        "sse_strictly_decreased": ev["strict_drop"],
        "max_error_before_mm": round(ev["max_error_before"], 4),
        "max_error_after_mm": round(ev["max_error_after"], 4),
        "all_pair_errors_within_1mm": ev["all_within_tolerance"],
        "conflicts": ev["conflicts"],
        "poses": [
            {
                "fragment_id": p["fragment_id"],
                "name": p["name"],
                "anchor": p["anchor"],
                "moved": p["moved"],
                "before": {
                    "x": r4(p["before"]["x"]),
                    "y": r4(p["before"]["y"]),
                    "theta_deg": r4(p["before"]["theta_deg"]),
                },
                "after": {
                    "x": r4(p["after"]["x"]),
                    "y": r4(p["after"]["y"]),
                    "theta_deg": r4(p["after"]["theta_deg"]),
                },
            }
            for p in ev["poses"]
        ],
        "relations": [
            {
                "relation_id": r["relation_id"],
                "fragment_a": r["fragment_a"],
                "fragment_b": r["fragment_b"],
                "name_a": r["name_a"],
                "name_b": r["name_b"],
                "marker_pairs": r["marker_pairs"],
                "errors_before": [round(e, 4) for e in r["errors_before"]],
                "errors_after": [round(e, 4) for e in r["errors_after"]],
                "max_error_before_mm": (round(r["max_error_before"], 4)
                                        if r["max_error_before"] is not None
                                        else None),
                "max_error_after_mm": (round(r["max_error_after"], 4)
                                       if r["max_error_after"] is not None
                                       else None),
            }
            for r in ev["relations"]
        ],
        "overlaps": [
            {"fragment_a": o["fragment_a"], "fragment_b": o["fragment_b"],
             "name_a": o["name_a"], "name_b": o["name_b"],
             "area_mm2": round(o["area_mm2"], 6)}
            for o in ev["overlaps"]
        ],
    }


@app.post("/api/calibration/preview")
def preview_calibration(payload: CalibrationIn):
    """锚点整体校准预览：固定锚点，仅平移+旋转其余碎片共同最小化误差。

    返回调整前后各片位姿、逐关系逐对误差、误差总量与轮廓冲突；不落库。
    版本过期 → 409；锚点不存在 → 404；所在组不足两片 → 400。
    """
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        try:
            ev = evaluate_calibration(d, payload.anchor_id)
        except CalibrationError as e:
            raise HTTPException(e.status, e.detail())
        return _calibration_response(
            payload.anchor_id, payload.expected_version, ev)


@app.post("/api/calibration/confirm", status_code=201)
def confirm_calibration(payload: CalibrationIn):
    """确认锚点整体校准：重新演算并校验，原子更新其余碎片位姿与关系误差。

    锚点位姿、轮廓、标记、关系状态与连通分组不变；仅当误差总量严格下降、
    每对对应点误差 ≤ 1 mm 且组内轮廓内部不重叠时才写入，版本只 +1 并留下
    source=calibration 的不可改快照；否则整次拒绝（不写入任何变化）。
    版本过期 → 409；锚点不存在 → 404；组不足两片 → 400。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            try:
                ev = evaluate_calibration(d, payload.anchor_id)
            except CalibrationError as e:
                raise HTTPException(e.status, e.detail())
            if not ev["ok"]:
                raise HTTPException(400, detail={
                    "message": "整体校准未满足确认条件，未写入任何变化，保持现状",
                    **_calibration_response(
                        payload.anchor_id, payload.expected_version, ev),
                })
            for fid, pose in ev["new_poses"].items():
                d.update_pose(fid, pose)
            for r in ev["relations"]:
                # 标记未变：仅刷新每条有效关系的最新最大误差
                d.update_relation_side(
                    r["relation_id"], None, None, r["max_error_after"])
            # 不调用 recompute_groups：拓扑未变，分组必须保持不变
            new_version = d.bump_version()
            d.save_snapshot(new_version, "calibration", d.assembly_state())
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "anchor_id": payload.anchor_id,
            "version": new_version, "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 拼接 API

@app.post("/api/relations/preview")
def preview_relation(payload: PreviewIn):
    """预览：仅平移+旋转后的组合，返回各片位置、角度及冲突原因，不落库。"""
    d = get_db()
    with d.lock:
        fa, fb = _load_pair(d, payload.fragment_a, payload.fragment_b)
        ev = evaluate_proposal(d, fa, fb, payload.markers_a, payload.markers_b)
        if not ev["involved"]:
            return {"ok": False, "relation_type": None,
                    "conflicts": ev["conflicts"], "errors": [],
                    "max_error_mm": None, "placements": [], "markers_world": None}
        placements = []
        for fid in ev["involved"]:
            f = ev["by_id"][fid]
            x, y, th = ev["poses"][fid]
            placements.append({
                "fragment_id": fid,
                "name": f["name"],
                "x": round(x, 4),
                "y": round(y, 4),
                "theta_deg": round(math.degrees(th), 4),
                "contour_world": geometry.contour_world(f["contour"], (x, y, th)),
            })
        ma, mb = ev["markers"]
        pa, pb = ev["poses"][fa["id"]], ev["poses"][fb["id"]]
        return {
            "ok": ev["ok"],
            "relation_type": ev["relation_type"],
            "conflicts": ev["conflicts"],
            "errors": [round(e, 4) for e in ev["errors"]],
            "max_error_mm": round(max(ev["errors"]), 4) if ev["errors"] else None,
            "placements": placements,
            "markers_world": {
                "a": [geometry.apply_pose(m, pa) for m in ma],
                "b": [geometry.apply_pose(m, pb) for m in mb],
            },
        }


@app.post("/api/relations", status_code=201)
def accept_relation(payload: AcceptIn):
    """采纳拼接：全部校验通过才落库，任一失败整次拒绝、原拼接组不变。"""
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            fa, fb = _load_pair(d, payload.fragment_a, payload.fragment_b)
            ev = evaluate_proposal(d, fa, fb, payload.markers_a, payload.markers_b)
            if not ev["ok"]:
                raise HTTPException(400, detail={
                    "message": "拼接被拒绝，原拼接组不变",
                    "conflicts": ev["conflicts"],
                })
            for fid, pose in ev["new_poses"].items():
                d.update_pose(fid, pose)
            rid = d.insert_relation(fa["id"], fb["id"], ev["markers"][0],
                                    ev["markers"][1], max(ev["errors"]))
            d.recompute_groups()
            v = d.bump_version()
            d.save_snapshot(v, "accept", d.assembly_state())
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "relation_id": rid, "version": v,
            "assembly": d.assembly_state()}


@app.post("/api/relations/{rid}/undo")
def undo_relation(rid: int, payload: UndoIn):
    """撤销关系：按剩余关系重算连通组，孤片恢复独立状态。"""
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            rel = d.get_relation(rid)
            if rel is None:
                raise HTTPException(404, f"拼接关系 #{rid} 不存在")
            if not rel["active"]:
                raise HTTPException(
                    409, f"版本冲突：拼接关系 #{rid} 已被撤销，请刷新后重试")
            d.undo_relation(rid)
            d.recompute_groups()
            v = d.bump_version()
            d.save_snapshot(v, "undo", d.assembly_state())
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "version": v, "assembly": d.assembly_state()}


@app.get("/api/relations")
def list_relations():
    return get_db().all_relations()


@app.get("/api/assembly")
def get_assembly():
    """当前装配快照：版本号、碎片位姿、连通组、全部关系。"""
    return get_db().assembly_state()


# ---------------------------------------------------------------- 有序试拼

def _rehearse_response(sim: dict) -> dict:
    """把推演结果整理成 /trials/rehearse 响应（不含内部 actions）。"""
    return {
        "ok": sim["ok"],
        "base_version": sim["base_version"],
        "steps": sim["steps"],
        "failed_at": sim["failed_at"],
        "failure": sim["failure"],
        "assembly": sim["assembly"],
    }


@app.post("/api/trials/rehearse")
def rehearse_trial(payload: TrialIn):
    """试拼预演：基于 expected_version 快照逐步推演，不改数据。

    返回每步结果（位姿/误差/连通组）、失败步及失败原因，以及最终（或失败
    前一刻）的位姿与分组。版本过期返回 409。
    """
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        try:
            sim = trial.simulate(d, payload.steps)
        except TrialError as e:
            raise HTTPException(e.status, e.detail())
    return _rehearse_response(sim)


@app.post("/api/trials/commit", status_code=201)
def commit_trial(payload: TrialIn):
    """确认试拼：同一版本 + 同一操作序列整体提交。

    任一步失败或版本过期均不写入任何变化；全部成功仅递增一次装配版本。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            try:
                sim = trial.simulate(d, payload.steps)
            except TrialError as e:
                raise HTTPException(e.status, e.detail())
            if not sim["ok"]:
                raise HTTPException(
                    sim["failure"]["http_status"], detail={
                        "message": "试拼序列存在失败步骤，未写入任何变化",
                        "failed_at": sim["failed_at"],
                        **{k: v for k, v in sim["failure"].items()
                           if k != "http_status"},
                    })
            cid_map = trial.commit_actions(d, sim)
            new_version = d.bump_version()
            d.save_snapshot(new_version, "trial", d.assembly_state())
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "version": new_version, "committed_steps": len(sim["steps"]),
            "relation_ids": cid_map, "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 历史版本

def _load_snapshot(d: DB, version: int):
    """取目标版本快照行与状态；不存在抛 404 version_not_found。"""
    rows = d.conn.execute(
        "SELECT version, source, created_at FROM snapshots WHERE version = ?",
        (version,)).fetchall()
    if not rows:
        raise HTTPException(
            404, detail={
                "message": f"目标版本 v{version} 的快照不存在",
                "code": "version_not_found",
                "version": version,
                "current_version": d.version(),
            })
    meta = rows[0]
    snap = d.get_snapshot_state(version)
    return meta["source"], meta["created_at"], snap


@app.get("/api/assembly/versions")
def list_versions():
    """历史版本清单：版本号、变更来源、快照时间（新版本在前）。

    快照不可改、不可删；基线（启用时记录）为最早一条。
    """
    d = get_db()
    with d.lock:
        items = d.list_snapshot_versions()
        current = d.version()
    for it in items:
        it["source_label"] = history.source_label(it["source"])
        it["current"] = it["version"] == current
    return {"current_version": current, "versions": items}


@app.get("/api/assembly/versions/{version}")
def get_version(version: int):
    """查看某历史版的完整装配：碎片轮廓/位姿、连通组、有效及已撤销关系。

    只读，不改当前数据。目标版本快照不存在 → 404 version_not_found。
    """
    d = get_db()
    with d.lock:
        source, created_at, snap = _load_snapshot(d, version)
        return history.build_history_view(version, source, created_at, snap)


@app.post("/api/assembly/versions/{version}/gaps")
def query_gaps(version: int, payload: GapsIn):
    """陶片对间隙查询：依据指定版本快照的固定轮廓与位姿，逐对计算轮廓边界
    的最短间隙、对应最近点坐标及是否内部重叠，按间隙从小到大、陶片编号
    排序返回。

    只读：不修改位姿、关系或历史，不产生新快照。整次拒绝并指出字段：
    空列表 / 无效编号 / 同片成对 / 重复陶片对 / 跨版本 → 400；版本快照
    缺失 → 404 version_not_found；快照中缺少陶片 → 404 fragment_not_found。
    间隙为 0 表示接触；负值（-1）仅标识内部重叠，不得当作可用间隙。
    """
    d = get_db()
    with d.lock:
        try:
            pairs = gaps.validate_pairs(version, payload.pairs)
        except gaps.GapError as e:
            raise HTTPException(e.status, e.detail())
        source, created_at, snap = _load_snapshot(d, version)
        try:
            rows = gaps.evaluate_gaps(snap, pairs)
        except gaps.GapError as e:
            raise HTTPException(e.status, e.detail())
        out = gaps.sort_rows([gaps.format_row(r) for r in rows])
        current = d.version()
        return {
            "ok": True,
            "version": version,
            "source": source,
            "source_label": history.source_label(source),
            "snapshot_created_at": created_at,
            "current_version": current,
            "historical": version != current,
            "pair_count": len(out),
            "pairs": out,
        }


@app.post("/api/assembly/versions/{version}/restore/preview")
def preview_restore(version: int, payload: RestoreIn):
    """恢复预览：展示恢复到该版后的完整装配及与当前版的碎片/关系差异。

    不写库。期望版本过期 → 409；目标版不存在 → 404 version_not_found；
    目标版即当前版 → 400（无需恢复）。
    """
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        source, created_at, snap = _load_snapshot(d, version)
        if version == d.version():
            raise HTTPException(
                400, detail={
                    "message": f"目标版本 v{version} 即当前版本，无需恢复",
                    "code": "target_is_current",
                    "current_version": d.version(),
                })
        return history.build_restore_preview(
            d.assembly_state(), version, source, created_at, snap)


@app.post("/api/assembly/versions/{version}/restore/confirm", status_code=201)
def confirm_restore(version: int, payload: RestoreIn):
    """确认恢复：原子恢复目标版碎片与关系的原 ID 及状态。

    - 必须提交预览所依据的当前版本号（expected_version），过期 → 409；
    - 目标版快照不存在 → 404，不写入；目标版即当前版 → 400，不写入；
    - 成功后重算连通组，装配版本只 +1，并留下 source=restore 的新快照；
      旧快照全部保留仍可查；关系与碎片的原 ID、active/undone_at/created_at
      及轮廓、标记、位姿均按该版原样恢复。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            _source, _created_at, snap = _load_snapshot(d, version)
            current = d.version()
            if version == current:
                raise HTTPException(
                    400, detail={
                        "message": f"目标版本 v{version} 即当前版本，无需恢复",
                        "code": "target_is_current",
                        "current_version": current,
                    })
            d.restore_state(snap)
            d.recompute_groups()
            new_version = d.bump_version()
            restored = d.assembly_state()
            d.save_snapshot(new_version, "restore", restored)
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {
        "ok": True,
        "restored_from_version": version,
        "expected_version": payload.expected_version,
        "version": new_version,
        "assembly": restored,
    }


# ---------------------------------------------------------------- 离线工作站迁移

def _validate_package(raw: dict) -> dict:
    """整包校验；不通过按主原因抛 400（detail 含全部拒绝原因）。"""
    try:
        return portable.validate_package(raw)
    except PackageError as e:
        raise HTTPException(e.status, e.detail())


@app.get("/api/transfer/export")
def export_package(download: bool = False):
    """导出当前装配及全部不可改版本快照为带格式版本号的 JSON 包。

    只读：不改当前数据、不产生新快照。download=true 时以附件下载
    （文件名含导出时间与当前版本），否则直接返回 JSON。
    """
    d = get_db()
    with d.lock:
        exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pkg = portable.build_package(d, exported_at)
    body = json.dumps(pkg, ensure_ascii=False, indent=2).encode("utf-8")
    if download:
        stamp = exported_at.replace(":", "-")
        fname = f"pottery-assembly-v{pkg['current_version']}-{stamp}.json"
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'})
    return Response(content=body, media_type="application/json")


@app.post("/api/transfer/import/preview")
def preview_import(payload: TransferPreviewIn):
    """导入预览（不写库）：包内碎片、有效及已撤销关系、版本数与导入后装配。

    包格式 / 轮廓 / 关系引用 / 快照版本 / 末版一致性问题 → 400，detail
    含 code 与全部 reasons；目标库不是空白基线时 HTTP 200 但 ok=false
    （确认按钮禁用，确认端点同样整次拒绝）。
    """
    d = get_db()
    pkg = _validate_package(payload.package)
    with d.lock:
        _is_blank, target = portable.target_is_blank(d)
    return portable.build_import_preview(pkg, target)


@app.post("/api/transfer/import/confirm", status_code=201)
def confirm_import(payload: TransferConfirmIn):
    """确认导入：单一事务内把整包写入空白目标库。

    - 携带目标库当前装配版本 expected_version；过期 → 409，库与历史原状；
    - 目标库仅允许存在空白基线（无用户碎片 / 关系，基线快照也为空），
      该基线由导入包整包替换；非空白 → 400 target_not_blank；
    - 保留碎片 / 关系原 ID、轮廓、位姿、标记、状态（active/undone_at/
      created_at）及全部快照原版本号、来源与快照时间戳；导入不产生新
      版本号、不新增快照，装配版本置为包内 current_version；
    - 包本身格式版本不支持 / 轮廓无效 / 关系引用缺失 / 快照版本重复 /
      末版与包内当前装配不一致 → 400 整包拒绝并指出原因。
    """
    d = get_db()
    pkg = _validate_package(payload.package)
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            is_blank, target = portable.target_is_blank(d)
            if not is_blank:
                raise HTTPException(400, detail={
                    "message": "目标库不是空白工作站，整包拒绝，库与历史保持原状",
                    "code": "target_not_blank",
                    "target": target,
                })
            d.import_package(pkg)
            d.recompute_groups()
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，导入未生效，库与历史保持原状")
    return {
        "ok": True,
        "imported_version": pkg["current_version"],
        "snapshot_count": len(pkg["snapshots"]),
        "expected_version": payload.expected_version,
        "assembly": d.assembly_state(),
    }


# ---------------------------------------------------------------- 前端静态页

app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
          name="static")
