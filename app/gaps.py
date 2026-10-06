"""陶片对轮廓间隙查询：按指定装配版本快照计算每对陶片的边界最短间隙。

修复师选择一个装配版本与若干陶片对，系统依据该版本快照中**固定的轮廓与
位姿**逐对计算（世界坐标，毫米制）：

- 两片轮廓**边界**之间的最短间隙 ``gap_mm``；
- 对应的最近点坐标（分别位于两片轮廓边界上）；
- 是否发生内部重叠（允许共边/共点，面积重叠才算重叠）。

间隙语义：``0`` 表示两边界接触；负值（固定 ``-1``）**仅用于标识内部重叠**，
无物理意义，不得当作可用间隙。结果按间隙从小到大、再按陶片编号
（fragment_a、fragment_b）排序返回。

本模块只做**只读**演算：不修改位姿、关系或历史，也不产生新版本快照。
请求级校验整次拒绝并逐字段指出问题：空列表、无效编号（非正整数）、同片
成对、重复陶片对（不分先后）、陶片对自带与请求不同的版本（跨版本）、
版本快照缺失、快照中缺少陶片。
"""
from __future__ import annotations

from shapely.geometry import Polygon
from shapely.ops import nearest_points

from . import geometry

# 重叠时返回的间隙标识值：负值仅表示「发生内部重叠」，不得当作可用间隙
OVERLAP_GAP_MM = -1.0
# 接触判定容差（mm）：边界距离不超过该值视为接触（间隙为零）
CONTACT_TOL_MM = 1e-6


class GapError(Exception):
    """请求级错误（整次拒绝），status 为对应 HTTP 状态码。

    errors 为逐字段错误明细：[{"field", "code", "message"}, ...]，
    field 形如 "pairs[0].fragment_a"，指出请求体中出问题的字段。
    """

    def __init__(self, status: int, code: str, message: str,
                 errors: list[dict] | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or []
        self.extra = extra or {}

    def detail(self) -> dict:
        d = {"message": self.message, "code": self.code}
        if self.errors:
            d["errors"] = self.errors
        d.update(self.extra)
        return d


def _field(index: int, name: str | None = None) -> str:
    return f"pairs[{index}]" if name is None else f"pairs[{index}].{name}"


# ---------------------------------------------------------------- 请求校验

def validate_pairs(version: int, pairs) -> list[dict]:
    """静态校验陶片对列表（与快照内容无关），不通过抛 GapError(400)。

    收集全部字段错误后一次性拒绝（整次拒绝、逐字段指出）；返回规范化后的
    [{"fragment_a", "fragment_b"}, ...]（保持提交顺序）。
    """
    if not pairs:
        raise GapError(
            400, "empty_pairs", "陶片对列表不能为空，整次拒绝",
            errors=[{"field": "pairs", "code": "empty_pairs",
                     "message": "至少需要提交一对陶片"}])
    errors: list[dict] = []
    seen: dict[frozenset, int] = {}
    norm: list[dict] = []
    for i, p in enumerate(pairs):
        fa, fb = p.fragment_a, p.fragment_b
        bad = False
        for name, fid in (("fragment_a", fa), ("fragment_b", fb)):
            if fid < 1:
                errors.append({
                    "field": _field(i, name), "code": "invalid_fragment_id",
                    "message": f"无效编号：碎片 id 必须为正整数（当前为 {fid}）"})
                bad = True
        if bad:
            continue
        if fa == fb:
            errors.append({
                "field": _field(i), "code": "same_fragment",
                "message": f"陶片对的两片相同（#{fa}），无法与自身计算间隙"})
            continue
        if p.version is not None and p.version != version:
            errors.append({
                "field": _field(i, "version"), "code": "cross_version",
                "message": f"跨版本：本对指定版本 v{p.version}，与请求版本 "
                           f"v{version} 不一致；仅允许查询同一版本快照"})
            continue
        key = frozenset((fa, fb))
        if key in seen:
            errors.append({
                "field": _field(i), "code": "duplicate_pair",
                "message": f"重复陶片对：#{fa} 与 #{fb} 已出现于 "
                           f"pairs[{seen[key]}]（不分先后）"})
            continue
        seen[key] = i
        norm.append({"fragment_a": fa, "fragment_b": fb})
    if errors:
        raise GapError(400, "invalid_pairs",
                       "陶片对校验失败，整次拒绝（未执行任何计算）",
                       errors=errors)
    return norm


# ---------------------------------------------------------------- 间隙演算

def evaluate_gaps(snap: dict, pairs: list[dict]) -> list[dict]:
    """在版本快照 snap 上逐对计算间隙（纯只读演算，不写库）。

    快照中缺少任一陶片时整次拒绝（GapError 404，逐字段指出）；
    返回未排序、未舍入的结果行。
    """
    frags = {int(f["id"]): f for f in snap["fragments"]}
    missing: list[dict] = []
    for i, p in enumerate(pairs):
        for name, fid in (("fragment_a", p["fragment_a"]),
                          ("fragment_b", p["fragment_b"])):
            if fid not in frags:
                missing.append({
                    "field": _field(i, name), "code": "fragment_not_found",
                    "message": f"碎片 #{fid} 在版本 v{snap['version']} "
                               "快照中不存在"})
    if missing:
        raise GapError(404, "fragment_not_found",
                       f"版本 v{snap['version']} 快照中缺少所需陶片，整次拒绝",
                       errors=missing, extra={"version": snap["version"]})
    return [_pair_row(frags[p["fragment_a"]], frags[p["fragment_b"]])
            for p in pairs]


def _pair_row(fa: dict, fb: dict) -> dict:
    """计算一对陶片（快照轮廓 + 快照位姿）的边界间隙、最近点与重叠情况。"""
    wa = geometry.contour_world(fa["contour"], (fa["x"], fa["y"], fa["theta"]))
    wb = geometry.contour_world(fb["contour"], (fb["x"], fb["y"], fb["theta"]))
    pa, pb = Polygon(wa), Polygon(wb)
    area = pa.intersection(pb).area
    overlap = area > geometry.OVERLAP_AREA_TOL
    dist = pa.boundary.distance(pb.boundary)
    na, nb = nearest_points(pa.boundary, pb.boundary)
    return {
        "fragment_a": fa["id"],
        "fragment_b": fb["id"],
        "name_a": fa["name"],
        "name_b": fb["name"],
        "gap_mm": OVERLAP_GAP_MM if overlap else dist,
        "contact": (not overlap) and dist <= CONTACT_TOL_MM,
        "overlap": overlap,
        "overlap_area_mm2": area if overlap else 0.0,
        "point_a": (na.x, na.y),
        "point_b": (nb.x, nb.y),
    }


# ---------------------------------------------------------------- 响应整理

def _r4(v: float) -> float:
    v = round(v, 4)
    return 0.0 if v == 0 else v   # 归一 -0.0，避免界面显示 -0.00


def format_row(row: dict) -> dict:
    """舍入并规范化响应行：间隙保留 4 位小数；显示为 0 即表示接触；
    重叠时固定为 -1（负值仅标识重叠，不得当作可用间隙）。"""
    if row["overlap"]:
        gap, contact = OVERLAP_GAP_MM, False
    else:
        gap = _r4(row["gap_mm"])
        contact = row["contact"] or gap == 0.0
        if contact:
            gap = 0.0
    return {
        "fragment_a": row["fragment_a"],
        "fragment_b": row["fragment_b"],
        "name_a": row["name_a"],
        "name_b": row["name_b"],
        "gap_mm": gap,
        "contact": contact,
        "overlap": row["overlap"],
        "overlap_area_mm2": round(row["overlap_area_mm2"], 6),
        "point_a": [_r4(row["point_a"][0]), _r4(row["point_a"][1])],
        "point_b": [_r4(row["point_b"][0]), _r4(row["point_b"][1])],
    }


def sort_rows(rows: list[dict]) -> list[dict]:
    """按间隙从小到大、再按陶片编号（fragment_a、fragment_b）排序。"""
    return sorted(rows, key=lambda r: (r["gap_mm"], r["fragment_a"],
                                       r["fragment_b"]))
