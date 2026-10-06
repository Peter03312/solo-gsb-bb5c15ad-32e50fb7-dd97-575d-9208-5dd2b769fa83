"""锚点整体校准：固定一片，最小二乘共同降低连通组全部对应点误差。

修复师从当前装配选一片碎片作**锚点**，请求对其所在连通组做整体校准：

- 锚点的位姿（x/y/theta）保持不变，只允许调整组内**其余**碎片的平移与
  旋转；轮廓、边缘标记、关系状态（active / undone_at / created_at）、
  关系 id 与连通分组均不变；
- 以组内**全部有效关系**的成对边缘标记构造对应点，用高斯-牛顿（LM 阻尼）
  最小化全部对应点世界坐标距离的误差平方和；
- 仅当误差总量**严格下降**、每对对应点误差均 ≤ 1 mm，且组内轮廓内部不
  重叠（允许共边/共点）时才允许确认，否则返回原因并保持现状。

evaluate_calibration 只读数据库、不写入，预览与确认共用；确认时在锁与
事务内用同一锚点重新演算并重新校验，通过后原子更新其余碎片位姿及每条
有效关系的最新误差，装配版本仅递增一次并留下不可改快照。
"""
from __future__ import annotations

import math

from . import geometry

# 误差总量严格下降的判定容差（mm²）：小于该值视为「没有下降」
SSE_PROGRESS_TOL = 1e-9
# LM 阻尼因子初值与收缩/放大倍数
LM_LAMBDA0 = 1e-3
LM_LAMBDA_DOWN = 0.3
LM_LAMBDA_UP = 3.0
LM_MAX_ITERS = 30


class CalibrationError(Exception):
    """对象级错误（锚点不存在 / 组不足两片），status 为对应 HTTP 状态码。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict:
        return {"message": self.message, "code": self.code}


# ---------------------------------------------------------------- 连通组

def active_component(d, anchor_id: int) -> tuple[set[int], list[dict]]:
    """沿有效关系扩展锚点所在连通分量，返回 (成员 id 集合, 组内有效关系)。"""
    comp = {anchor_id}
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
    rels = [r for r in active_rels
            if r["fragment_a"] in comp and r["fragment_b"] in comp]
    return comp, rels


# ---------------------------------------------------------------- 线性代数

def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """高斯-若尔当解稠密线性方程组 A x = b；奇异返回 None。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        d = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= d
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if f:
                for j in range(col, n + 1):
                    m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


# ---------------------------------------------------------------- 位姿优化

def _residuals(poses: dict, rels: list[dict]) -> list[dict]:
    """每条有效关系的每对对应点，构造一条残差记录（世界坐标）。"""
    out: list[dict] = []
    for r in rels:
        pa, pb = poses[r["fragment_a"]], poses[r["fragment_b"]]
        for ma, mb in zip(r["markers_a"], r["markers_b"]):
            out.append({"rel": r, "ma": ma, "mb": mb,
                        "wa": geometry.apply_pose(ma, pa),
                        "wb": geometry.apply_pose(mb, pb),
                        "a": r["fragment_a"], "b": r["fragment_b"]})
    return out


def _sse_and_jacobian(poses: dict, order: list[int], rels: list[dict]):
    """计算误差平方和与高斯-牛顿法方程 JᵀJ x = -Jᵀr（稠密 3N 维）。

    order 为可调整碎片 id 顺序（锚点不在其中）；每个可调整碎片含
    (tx, ty, θ) 三个待估参数。
    """
    col = {fid: k * 3 for k, fid in enumerate(order)}
    n = len(order) * 3
    hess = [[0.0] * n for _ in range(n)]
    grad = [0.0] * n
    sse = 0.0
    for rec in _residuals(poses, rels):
        ex = rec["wa"][0] - rec["wb"][0]
        ey = rec["wa"][1] - rec["wb"][1]
        sse += ex * ex + ey * ey
        # 残差 e = (R(θ_a)p_a + t_a) − (R(θ_b)p_b + t_b)
        # 对每个可调端点 (tx, ty, θ) 的 2×3 雅可比（A 侧 +、B 侧 −）
        terms = []   # (fid, sign, J)
        for fid, p, sign in ((rec["a"], rec["ma"], 1.0),
                             (rec["b"], rec["mb"], -1.0)):
            th = poses[fid][2]
            c, s = math.cos(th), math.sin(th)
            j = ((1.0, 0.0, -s * p[0] - c * p[1]),
                 (0.0, 1.0, c * p[0] - s * p[1]))
            terms.append((fid, sign, j))
        # 梯度 g = Σ Jᵀr（按端点符号计入）
        # 海森 H = Σ JᵀJ（同号/异号端点通过 sign 乘积自然得到 ± 交叉块）
        js: dict[int, tuple[float, tuple]] = {}
        for fid, sign, j in terms:
            base = col.get(fid)
            if base is None:
                continue          # 锚点固定：不参与法方程
            js[base] = (sign, j)
            for i in range(3):
                grad[base + i] += sign * (j[0][i] * ex + j[1][i] * ey)
        for ba, (sa, ja) in js.items():
            for bb, (sb, jb) in js.items():
                if ba > bb:
                    continue
                factor = sa * sb
                for i in range(3):
                    for k in range(3):
                        v = factor * (ja[0][i] * jb[0][k]
                                      + ja[1][i] * jb[1][k])
                        hess[ba + i][bb + k] += v
                        if ba != bb:
                            hess[bb + k][ba + i] += v
    return sse, hess, grad


def fit_group_poses(frags: dict[int, dict], anchor_id: int,
                    rels: list[dict]) -> dict:
    """固定锚点位姿，LM 阻尼最小二乘求其余碎片的最优平移+旋转。

    返回 {fid: (x, y, theta)}（含锚点，锚点位姿与输入一致）。
    """
    poses = {fid: (f["x"], f["y"], f["theta"]) for fid, f in frags.items()}
    order = sorted(f for f in frags if f != anchor_id)
    if not order or not rels:
        return poses

    sse, hess, grad = _sse_and_jacobian(poses, order, rels)
    lam = LM_LAMBDA0
    for _ in range(LM_MAX_ITERS):
        a = [row[:] for row in hess]
        for i in range(len(a)):
            a[i][i] += lam
        step = _solve_linear(a, [-g for g in grad])
        if step is None:
            lam *= LM_LAMBDA_UP
            if lam > 1e12:
                break
            continue
        trial = dict(poses)
        for k, fid in enumerate(order):
            x, y, th = poses[fid]
            trial[fid] = (x + step[k * 3], y + step[k * 3 + 1],
                          th + step[k * 3 + 2])
        new_sse, new_hess, new_grad = _sse_and_jacobian(trial, order, rels)
        if new_sse < sse - 1e-12:
            poses, sse, hess, grad = trial, new_sse, new_hess, new_grad
            lam *= LM_LAMBDA_DOWN
            if max(abs(v) for v in step) < 1e-10:
                break
        else:
            lam *= LM_LAMBDA_UP
            if lam > 1e12:
                break
    return poses


# ---------------------------------------------------------------- 校验演算

def evaluate_calibration(d, anchor_id: int) -> dict:
    """演算一次锚点整体校准（只读不写）。

    锚点不存在抛 CalibrationError(404)；锚点为孤片（所在连通组不足两片）
    抛 CalibrationError(400)。
    """
    anchor = d.get_fragment(anchor_id)
    if anchor is None:
        raise CalibrationError(404, "fragment_not_found",
                               f"锚点碎片 #{anchor_id} 不存在")
    comp, rels = active_component(d, anchor_id)
    if len(comp) < 2:
        raise CalibrationError(
            400, "group_too_small",
            f"锚点碎片 #{anchor_id} 所在连通组不足两片（孤片无有效关系），"
            "无法整体校准")

    all_frags = {f["id"]: f for f in d.all_fragments()}
    frags = {fid: all_frags[fid] for fid in comp if fid in all_frags}

    before_poses = {fid: (f["x"], f["y"], f["theta"])
                    for fid, f in frags.items()}
    after_poses = fit_group_poses(frags, anchor_id, rels)

    # 逐关系逐对对应点误差（校准前 / 校准后）
    rel_rows: list[dict] = []
    before_sse = after_sse = 0.0
    max_before = max_after = 0.0
    for r in sorted(rels, key=lambda x: x["id"]):
        errs_before = geometry.pair_errors(
            r["markers_a"], r["markers_b"],
            before_poses[r["fragment_a"]], before_poses[r["fragment_b"]])
        errs_after = geometry.pair_errors(
            r["markers_a"], r["markers_b"],
            after_poses[r["fragment_a"]], after_poses[r["fragment_b"]])
        before_sse += sum(e * e for e in errs_before)
        after_sse += sum(e * e for e in errs_after)
        max_before = max(max_before, max(errs_before, default=0.0))
        max_after = max(max_after, max(errs_after, default=0.0))
        rel_rows.append({
            "relation_id": r["id"],
            "fragment_a": r["fragment_a"],
            "fragment_b": r["fragment_b"],
            "name_a": frags[r["fragment_a"]]["name"],
            "name_b": frags[r["fragment_b"]]["name"],
            "errors_before": errs_before,
            "errors_after": errs_after,
            "max_error_before": max(errs_before, default=None),
            "max_error_after": max(errs_after, default=None),
            "marker_pairs": len(errs_after),
        })

    # 组内轮廓内部重叠（校准后位姿）
    placed = [(fid, frags[fid]["name"],
               geometry.contour_world(frags[fid]["contour"], after_poses[fid]))
              for fid in sorted(comp)]
    overlaps = []
    for id1, id2, name1, name2, area in geometry.find_overlaps(placed):
        overlaps.append({"fragment_a": id1, "fragment_b": id2,
                         "name_a": name1, "name_b": name2,
                         "area_mm2": area})

    strict_drop = after_sse < before_sse - SSE_PROGRESS_TOL
    over_limit = [e for row in rel_rows for e in row["errors_after"]
                  if e > geometry.MAX_ERROR_MM + 1e-9]

    conflicts: list[str] = []
    if not strict_drop:
        conflicts.append(
            f"误差总量未严格下降：校准前 {before_sse:.6f} mm²，"
            f"校准后 {after_sse:.6f} mm²；当前位姿已（近似）最优，保持现状")
    for row in rel_rows:
        for i, e in enumerate(row["errors_after"]):
            if e > geometry.MAX_ERROR_MM + 1e-9:
                conflicts.append(
                    f"误差超限：关系 #{row['relation_id']} 第 {i + 1} 对对应点"
                    f"校准后误差 {e:.3f} mm，超过 {geometry.MAX_ERROR_MM:.0f} mm 限值")
    for o in overlaps:
        conflicts.append(
            f"碰撞：碎片「{o['name_a']}」与「{o['name_b']}」校准后轮廓内部"
            f"重叠 {o['area_mm2']:.2f} mm²")

    poses_out = []
    for fid in sorted(comp):
        bx, by, bth = before_poses[fid]
        ax, ay, ath = after_poses[fid]
        poses_out.append({
            "fragment_id": fid,
            "name": frags[fid]["name"],
            "anchor": fid == anchor_id,
            "before": {"x": bx, "y": by, "theta": bth,
                       "theta_deg": math.degrees(bth)},
            "after": {"x": ax, "y": ay, "theta": ath,
                      "theta_deg": math.degrees(ath)},
            "moved": fid != anchor_id,
        })

    return {
        "ok": not conflicts,
        "anchor_id": anchor_id,
        "involved": sorted(comp),
        "poses": poses_out,
        "relations": rel_rows,
        "overlaps": overlaps,
        "sse_before": before_sse,
        "sse_after": after_sse,
        "strict_drop": strict_drop,
        "max_error_before": max_before,
        "max_error_after": max_after,
        "all_within_tolerance": not over_limit,
        "conflicts": conflicts,
        "new_poses": {fid: after_poses[fid]
                      for fid in comp if fid != anchor_id},
    }
