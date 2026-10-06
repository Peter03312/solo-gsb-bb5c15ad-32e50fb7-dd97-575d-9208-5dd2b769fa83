"""碎片轮廓修订：提交修订轮廓 + 该片每条有效关系的新边缘标记。

修复师发现某片陶片轮廓测量有误时，可提交：
- 修订后的毫米制闭合轮廓（须通过既有的非自交校验）；
- 该片**每一条**有效拼接关系上、属于本片一侧的全新边缘标记
  （遗漏任何一条有效关系即整次拒绝）；
- 期望装配版本号（先预览再确认，过期返回 409）。

新标记的约束：位于修订轮廓边缘、数量与关系另一端一致、至少 2 处、
同片不重合。随后**保持现有位姿不变**，检查受影响连通组内全部有效关系：
- 每条关系逐对对应点误差 ≤ 1 mm；
- 组内成员轮廓（修订片用新轮廓）内部两两不重叠（允许共边/共点）。

独立碎片（无有效关系）只校验轮廓本身。

evaluate_revision 只读数据库、不写入，预览与确认共用；确认时在锁与事务内
用同一输入重新校验，通过后原子更新轮廓、相关标记与误差，位姿 / 关系 ID /
连通分组 / 已撤销历史均保持不变，装配版本仅递增一次。
"""
from __future__ import annotations

from . import geometry


class RevisionError(Exception):
    """对象级错误（如关系不存在），status 为对应的 HTTP 状态码。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict:
        return {"message": self.message, "code": self.code}


# ---------------------------------------------------------------- 连通组

def _component(d, fid: int) -> tuple[set[int], list[dict]]:
    """以 fid 为起点、沿有效关系扩展得到的连通分量（含片与组内有效关系）。"""
    comp = {fid}
    active_rels = [r for r in d.all_relations() if r["active"]]
    changed = True
    while changed:
        changed = False
        for r in active_rels:
            a, b = r["fragment_a"], r["fragment_b"]
            if (a in comp or b in comp) and (a not in comp or b not in comp):
                comp.add(a)
                comp.add(b)
                changed = True
    return comp, [r for r in active_rels
                  if r["fragment_a"] in comp and r["fragment_b"] in comp]


# ---------------------------------------------------------------- 校验演算

def evaluate_revision(d, fid: int, contour_in, rel_inputs) -> dict:
    """演算一次轮廓修订（只读不写）。rel_inputs: [(relation_id, markers), ...]。

    返回 ok / 全局 conflicts / 逐关系行（含逐条误差与该行冲突）/ 重叠明细 /
    确认时需要落库的 updates（仅 ok=True 时有内容）。
    关系 id 在库中不存在时抛 RevisionError(404)。
    """
    conflicts: list[str] = []

    # 1) 修订轮廓：沿用录入碎片的同一套非自交校验
    try:
        contour = geometry.validate_contour(contour_in)
        contour_ok = True
    except geometry.GeometryError as e:
        contour = None
        contour_ok = False
        conflicts.append(str(e))

    all_rels = {r["id"]: r for r in d.all_relations()}
    incident = [r for r in all_rels.values()
                if r["active"]
                and (r["fragment_a"] == fid or r["fragment_b"] == fid)]
    incident.sort(key=lambda r: r["id"])

    # 2) 提交关系清单的静态校验：未知 / 已撤销 / 不涉及本片 / 重复提交
    inputs: dict[int, list] = {}
    for rid, markers in rel_inputs:
        if rid in inputs:
            conflicts.append(f"拼接关系 #{rid} 的修订标记重复提交，每条关系只能出现一次")
            continue
        rel = all_rels.get(rid)
        if rel is None:
            raise RevisionError(404, "relation_not_found",
                                f"拼接关系 #{rid} 不存在")
        if not rel["active"]:
            conflicts.append(f"拼接关系 #{rid} 已撤销，不能随轮廓修订更新其标记")
            continue
        if rel["fragment_a"] != fid and rel["fragment_b"] != fid:
            conflicts.append(f"拼接关系 #{rid} 不涉及碎片 #{fid}，"
                             "不应在本次修订中提交其标记")
            continue
        inputs[rid] = markers

    # 3) 遗漏有效关系：本片每条有效关系都必须提交新标记
    missing = [r["id"] for r in incident if r["id"] not in inputs]
    for rid in missing:
        conflicts.append(
            f"遗漏有效拼接关系 #{rid}：修订轮廓时必须一并提交本片在该关系上的新边缘标记")

    # 4) 逐关系校验本片新标记：数量一致 / ≥2 / 不重合 / 位于修订轮廓边缘
    rows: dict[int, dict] = {}
    revised: dict[int, tuple[str, list]] = {}   # rid -> (side, 规范化标记)
    for r in incident:
        rid = r["id"]
        side = "a" if r["fragment_a"] == fid else "b"
        other_id = r["fragment_b"] if side == "a" else r["fragment_a"]
        other_markers = r["markers_b"] if side == "a" else r["markers_a"]
        submitted = inputs.get(rid)
        row = {
            "relation_id": rid,
            "revised": submitted is not None,
            "side": side if submitted is not None else None,
            "fragment_a": r["fragment_a"],
            "fragment_b": r["fragment_b"],
            "other_fragment_id": other_id,
            "required_count": len(other_markers),
            "marker_count": len(submitted) if submitted is not None else None,
            "errors": [],
            "max_error_mm": None,
            "conflicts": [],
            "markers_a": r["markers_a"],
            "markers_b": r["markers_b"],
        }
        if submitted is not None:
            if len(submitted) != len(other_markers):
                row["conflicts"].append(
                    f"标记数量须与关系另一端一致：本片提交 {len(submitted)} 处，"
                    f"另一端为 {len(other_markers)} 处")
            if contour_ok:
                try:
                    pts = geometry.validate_side_markers(submitted, contour)
                    if len(pts) == len(other_markers):
                        revised[rid] = (side, pts)
                        if side == "a":
                            row["markers_a"] = pts
                        else:
                            row["markers_b"] = pts
                except geometry.GeometryError as e:
                    row["conflicts"].append(str(e))
            for c in row["conflicts"]:
                conflicts.append(f"［关系 #{rid}］{c}")
        rows[rid] = row

    # 5) 受影响连通组：以现有位姿检查全部有效关系的对应点误差
    comp, comp_rels = _component(d, fid)
    frags = {f["id"]: f for f in d.all_fragments()}

    def pose_of(x):
        f = frags[x]
        return (f["x"], f["y"], f["theta"])

    def effective_markers(r):
        if r["id"] in revised:
            side, pts = revised[r["id"]]
            return (pts, r["markers_b"]) if side == "a" else (r["markers_a"], pts)
        return r["markers_a"], r["markers_b"]

    for r in sorted(comp_rels, key=lambda x: x["id"]):
        rid = r["id"]
        row = rows.get(rid)
        if row is None:
            # 组内不直接涉及修订片的关系：标记与位姿都没变，仍按要求逐条核验
            row = {
                "relation_id": rid,
                "revised": False,
                "side": None,
                "fragment_a": r["fragment_a"],
                "fragment_b": r["fragment_b"],
                "other_fragment_id": None,
                "required_count": None,
                "marker_count": None,
                "errors": [],
                "max_error_mm": None,
                "conflicts": [],
                "markers_a": r["markers_a"],
                "markers_b": r["markers_b"],
            }
            rows[rid] = row
        a, b = r["fragment_a"], r["fragment_b"]
        if a not in frags or b not in frags:
            continue
        ma, mb = effective_markers(r)
        if len(ma) != len(mb):
            continue   # 数量不符的行已有冲突，无法配对计算
        errs = geometry.pair_errors(ma, mb, pose_of(a), pose_of(b))
        row["errors"] = errs
        row["max_error_mm"] = max(errs) if errs else None
        for i, e in enumerate(errs):
            if e > geometry.MAX_ERROR_MM + 1e-9:
                msg = (f"误差超限：关系 #{rid} 第 {i + 1} 对对应点误差 "
                       f"{e:.3f} mm，超过 {geometry.MAX_ERROR_MM:.0f} mm 限值"
                       f"（位姿保持不变）")
                row["conflicts"].append(msg)
                conflicts.append(f"［关系 #{rid}］{msg}")

    # 6) 组成员轮廓内部不重叠（修订片用新轮廓，其余用现轮廓，位姿全部不变）
    overlaps: list[dict] = []
    if contour_ok and len(comp) > 1:
        placed = []
        for cid in sorted(comp):
            f = frags.get(cid)
            if f is None:
                continue
            c = contour if cid == fid else f["contour"]
            placed.append((cid, f["name"],
                           geometry.contour_world(c, pose_of(cid))))
        for id1, id2, name1, name2, area in geometry.find_overlaps(placed):
            overlaps.append({"fragment_a": id1, "fragment_b": id2,
                             "name_a": name1, "name_b": name2,
                             "area_mm2": area})
            conflicts.append(
                f"碰撞：碎片「{name1}」与「{name2}」轮廓内部重叠 "
                f"{area:.2f} mm²（位姿保持不变）")

    ordered_rows = [rows[rid] for rid in sorted(rows)]
    updates = {}
    if not conflicts:
        for rid, row in rows.items():
            if rid not in {r["id"] for r in comp_rels}:
                continue
            if rid in revised:
                side, pts = revised[rid]
                updates[rid] = {"side": side, "markers": pts,
                                "max_error": row["max_error_mm"]}
            elif row["max_error_mm"] is not None:
                # 组内不涉及修订片的关系：标记不变，仅以现有位姿刷新误差
                updates[rid] = {"side": None, "markers": None,
                                "max_error": row["max_error_mm"]}

    return {
        "ok": not conflicts,
        "fragment_id": fid,
        "contour": contour,
        "independent": not incident,
        "conflicts": conflicts,
        "missing_relation_ids": missing,
        "relations": ordered_rows,
        "overlaps": overlaps,
        "involved": sorted(comp),
        "updates": updates,
    }
