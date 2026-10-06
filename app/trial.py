"""有序试拼：基于装配版本快照的纯内存逐步推演与整组提交。

修复师可编排一个非空、有序的操作序列，每步二者之一：
- ``relate`` 以两片陶片及成对边缘标记建立关系（沿用单条采纳的全部规则：
  刚体配准、对应点误差 ≤ 1mm、同组轮廓内部不重叠、闭环须符合已有位姿）；
- ``undo``   撤销一条现存有效关系（撤销后按剩余关系重算连通组）。

预演（rehearse）从数据库当前版本复制一份内存快照逐步推演，全程不写库；
确认（commit）时在锁与事务内重新校验版本并重放同一序列，任一步失败或
版本过期都整体回滚，全部成功仅把装配版本递增一次。

序列内新建的关系使用负的临时 id，并可通过 ``client_id`` 在后续步骤中
被撤销；落库后返回其真实关系 id。
"""
from __future__ import annotations

import math

from . import geometry
from .database import _now

# ---------------------------------------------------------------- 错误


class TrialError(Exception):
    """试拼步骤级错误（对象缺失 / 重复撤销 / 同片拼接等）。

    status 为确认提交时应使用的 HTTP 状态码；预演时随失败原因一并返回。
    """

    def __init__(self, status: int, code: str, message: str,
                 step: int | None = None, action: str | None = None,
                 extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.step = step
        self.action = action
        self.extra = extra or {}

    def detail(self) -> dict:
        d = {"message": self.message, "code": self.code}
        if self.step is not None:
            d["step"] = self.step
        if self.action is not None:
            d["action"] = self.action
        d.update(self.extra)
        return d


# ---------------------------------------------------------------- 位姿演算

def _pose_of(f: dict) -> tuple[float, float, float]:
    return (f["x"], f["y"], f["theta"])


def _members(state, frag: dict) -> list[dict]:
    """碎片所在组的全部成员（孤片即自身）。state 需提供 group_members。"""
    if frag["group_id"] is None:
        return [frag]
    return state.group_members(frag["group_id"])


def evaluate_proposal(state, fa: dict, fb: dict,
                      markers_a, markers_b) -> dict:
    """计算建立关系后的位姿方案（只读，不落库），预演/单条采纳/提交共用。

    state 只需实现 get_fragment / group_members（DB 与 SimState 均满足）。
    """
    try:
        ma, mb = geometry.validate_markers(markers_a, markers_b)
    except geometry.GeometryError as e:
        return {"ok": False, "relation_type": None, "conflicts": [str(e)],
                "errors": [], "poses": {}, "new_poses": {}, "involved": [],
                "markers": (markers_a, markers_b)}

    members_a, members_b = _members(state, fa), _members(state, fb)
    ga, gb = fa["group_id"], fb["group_id"]
    pose_a, pose_b = _pose_of(fa), _pose_of(fb)
    new_poses: dict[int, tuple] = {}

    if ga is not None and ga == gb:
        # 闭环关系：必须符合已有位姿，不移动任何碎片
        relation_type = "loop"
        involved = {f["id"] for f in members_a}
    else:
        dst = [geometry.apply_pose(m, pose_a) for m in ma]  # A 侧标记世界坐标
        if ga is None and gb is None:
            relation_type = "new_group"
            new_poses[fb["id"]] = geometry.fit_rigid(mb, dst)
        elif ga is not None and gb is None:
            relation_type = "join"
            new_poses[fb["id"]] = geometry.fit_rigid(mb, dst)
        elif ga is None and gb is not None:
            relation_type = "join"
            src_b = [geometry.apply_pose(m, pose_b) for m in mb]
            new_poses[fa["id"]] = geometry.fit_rigid(ma, src_b)
        else:
            # 合并两组：整体移动 B 组，组内相对位姿不变
            relation_type = "merge"
            src_b = [geometry.apply_pose(m, pose_b) for m in mb]
            T = geometry.fit_rigid(src_b, dst)
            for f in members_b:
                new_poses[f["id"]] = geometry.compose_pose(T, _pose_of(f))
        involved = {f["id"] for f in members_a} | {f["id"] for f in members_b}

    # 采纳后的最终位姿 = 当前位姿 + 本次改动
    by_id = {f["id"]: f for f in members_a + members_b}
    poses = {fid: _pose_of(f) for fid, f in by_id.items()}
    poses.update(new_poses)

    conflicts: list[str] = []
    errors = geometry.pair_errors(ma, mb, poses[fa["id"]], poses[fb["id"]])
    for i, e in enumerate(errors):
        if e > geometry.MAX_ERROR_MM + 1e-9:
            conflicts.append(
                f"误差超限：第 {i + 1} 对对应点误差 {e:.3f} mm，"
                f"超过 {geometry.MAX_ERROR_MM:.0f} mm 限值")

    placed = [
        (fid, by_id[fid]["name"],
         geometry.contour_world(by_id[fid]["contour"], poses[fid]))
        for fid in sorted(involved)
    ]
    for _k1, _k2, n1, n2, area in geometry.find_overlaps(placed):
        conflicts.append(
            f"碰撞：碎片「{n1}」与「{n2}」轮廓内部重叠 {area:.2f} mm²")

    return {
        "ok": not conflicts,
        "relation_type": relation_type,
        "conflicts": conflicts,
        "errors": errors,
        "poses": poses,
        "new_poses": new_poses,
        "involved": sorted(involved),
        "by_id": by_id,
        "markers": (ma, mb),
    }


# ---------------------------------------------------------------- 内存快照

class SimState:
    """装配版本的内存快照：碎片位姿 + 关系 + 连通组，接口形状与 DB 对齐。"""

    def __init__(self, d):
        self.base_version = d.version()
        self.frags: dict[int, dict] = {
            f["id"]: dict(f) for f in d.all_fragments()}
        self.relations: dict[int, dict] = {}
        for r in d.all_relations():
            rr = dict(r)
            rr["client_id"] = None
            self.relations[rr["id"]] = rr
        self._next_temp = -1
        self.cid_index: dict[str, int] = {}   # client_id -> 临时关系 key

    # 与 DB 对齐的查询接口
    def get_fragment(self, fid: int) -> dict | None:
        return self.frags.get(int(fid))

    def all_fragments(self) -> list[dict]:
        return [self.frags[i] for i in sorted(self.frags)]

    def group_members(self, group_id: int) -> list[dict]:
        return [f for f in self.all_fragments() if f["group_id"] == group_id]

    def get_relation(self, key: int) -> dict | None:
        return self.relations.get(int(key))

    def all_relations(self) -> list[dict]:
        return [self.relations[k] for k in sorted(self.relations)]

    # ------------------------------------------------------------ 写入
    def new_temp_key(self) -> int:
        key = self._next_temp
        self._next_temp -= 1
        return key

    def add_relation(self, fa: int, fb: int, ma, mb, max_error: float,
                     client_id: str | None) -> int:
        key = self.new_temp_key()
        self.relations[key] = {
            "id": key,
            "fragment_a": fa,
            "fragment_b": fb,
            "markers_a": ma,
            "markers_b": mb,
            "max_error": max_error,
            "active": True,
            "created_at": _now(),
            "undone_at": None,
            "client_id": client_id,
        }
        return key

    def apply_poses(self, new_poses: dict):
        for fid, pose in new_poses.items():
            f = self.frags[fid]
            f["x"], f["y"], f["theta"] = pose

    def deactivate(self, key: int):
        rel = self.relations[key]
        rel["active"] = False
        rel["undone_at"] = _now()

    # ------------------------------------------------------------ 连通组
    def recompute_groups(self):
        """按当前有效关系重算连通分量；孤片 group_id 置 NULL（与 DB 同规则）。"""
        parent = {i: i for i in self.frags}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for rel in self.all_relations():
            if not rel["active"]:
                continue
            a, b = rel["fragment_a"], rel["fragment_b"]
            if a in parent and b in parent:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

        comps: dict[int, list[int]] = {}
        for i in self.frags:
            comps.setdefault(find(i), []).append(i)
        for members in comps.values():
            gid = min(members) if len(members) > 1 else None
            for m in members:
                self.frags[m]["group_id"] = gid

    # ------------------------------------------------------------ 快照
    def groups(self) -> list[dict]:
        groups: dict[int, list[int]] = {}
        for f in self.all_fragments():
            if f["group_id"] is not None:
                groups.setdefault(f["group_id"], []).append(f["id"])
        return [{"id": gid, "fragment_ids": sorted(ids)}
                for gid, ids in sorted(groups.items())]

    def assembly(self) -> dict:
        """试拼推演后的装配快照（version 仍为所基于的版本，未落库）。"""
        return {
            "version": self.base_version,
            "rehearsal": True,
            "fragments": self.all_fragments(),
            "groups": self.groups(),
            "relations": self.all_relations(),
        }


# ---------------------------------------------------------------- 请求级校验

def validate_steps(steps):
    """序列本身的静态校验（与逐步状态无关），不通过抛 TrialError(400)。"""
    if not steps:
        raise TrialError(400, "empty_steps", "试拼操作序列不能为空")
    seen_cids: set[str] = set()
    for i, s in enumerate(steps, start=1):
        if s.action == "relate":
            cid = getattr(s, "client_id", None)
            if cid is not None:
                if cid in seen_cids:
                    raise TrialError(
                        400, "duplicate_client_id",
                        f"第 {i} 步的 client_id「{cid}」在序列中重复，"
                        "每个新建关系的 client_id 必须唯一",
                        step=i, action="relate")
                seen_cids.add(cid)
        else:
            rid, cid = s.relation_id, s.client_id
            if rid is not None and cid is not None:
                raise TrialError(
                    400, "invalid_undo_target",
                    f"第 {i} 步（撤销）只能指定 relation_id 或 client_id 之一，"
                    "不能同时提供", step=i, action="undo")
            if rid is None and cid is None:
                raise TrialError(
                    400, "invalid_undo_target",
                    f"第 {i} 步（撤销）必须指定 relation_id 或 client_id",
                    step=i, action="undo")


# ---------------------------------------------------------------- 逐步推演

def _load_pair(state: SimState, fa_id: int, fb_id: int, index: int):
    if fa_id == fb_id:
        raise TrialError(400, "same_fragment",
                         "不能与同一片陶片建立拼接关系",
                         step=index, action="relate")
    fa, fb = state.get_fragment(fa_id), state.get_fragment(fb_id)
    if fa is None or fb is None:
        missing = fa_id if fa is None else fb_id
        raise TrialError(404, "fragment_missing",
                         f"碎片 #{missing} 不存在", step=index,
                         action="relate")
    return fa, fb


def _placements(ev: dict) -> list[dict]:
    out = []
    for fid in ev["involved"]:
        f = ev["by_id"][fid]
        x, y, th = ev["poses"][fid]
        out.append({
            "fragment_id": fid,
            "name": f["name"],
            "x": round(x, 4),
            "y": round(y, 4),
            "theta_deg": round(math.degrees(th), 4),
        })
    return out


def _sim_relate(state: SimState, s, index: int) -> tuple[dict, dict]:
    """推演一步建立关系；返回 (步骤结果, 提交动作)。失败时动作不产生。"""
    fa, fb = _load_pair(state, s.fragment_a, s.fragment_b, index)
    ev = evaluate_proposal(state, fa, fb, s.markers_a, s.markers_b)
    result = {
        "index": index,
        "action": "relate",
        "fragment_a": s.fragment_a,
        "fragment_b": s.fragment_b,
        "client_id": s.client_id,
        "relation_type": ev["relation_type"],
        "errors": [round(e, 4) for e in ev["errors"]],
        "max_error_mm": round(max(ev["errors"]), 4) if ev["errors"] else None,
        "placements": _placements(ev) if ev["involved"] else [],
        "conflicts": ev["conflicts"],
    }
    if not ev["ok"]:
        result["ok"] = False
        result["relation_id"] = None
        result["groups"] = state.groups()
        return result, None

    ma, mb = ev["markers"]
    key = state.add_relation(
        fa["id"], fb["id"], ma, mb, max(ev["errors"]), s.client_id)
    if s.client_id is not None:
        state.cid_index[s.client_id] = key
    state.apply_poses(ev["new_poses"])
    state.recompute_groups()

    result.update(ok=True, relation_id=key, groups=state.groups())
    action = {
        "action": "relate",
        "fragment_a": fa["id"],
        "fragment_b": fb["id"],
        "markers_a": ma,
        "markers_b": mb,
        "max_error": max(ev["errors"]),
        "new_poses": dict(ev["new_poses"]),
        "client_id": s.client_id,
        "key": key,
    }
    return result, action


def _resolve_undo_target(state: SimState, s, index: int) -> int:
    if s.client_id is not None:
        key = state.cid_index.get(s.client_id)
        if key is None:
            raise TrialError(
                404, "client_relation_not_found",
                f"第 {index} 步要撤销的试拼关系「{s.client_id}」"
                "在之前的步骤中不存在（client_id 未找到）",
                step=index, action="undo")
        return key
    key = int(s.relation_id)
    if state.get_relation(key) is None:
        raise TrialError(404, "relation_not_found",
                         f"拼接关系 #{key} 不存在",
                         step=index, action="undo")
    return key


def _sim_undo(state: SimState, s, index: int) -> tuple[dict, dict]:
    """推演一步撤销；重复撤销 / 对象缺失抛 TrialError。"""
    key = _resolve_undo_target(state, s, index)
    rel = state.get_relation(key)
    if not rel["active"]:
        raise TrialError(
            409, "relation_inactive",
            f"拼接关系 #{key} 已被撤销，不能重复撤销",
            step=index, action="undo")

    before_comp = _components(state)
    state.deactivate(key)
    state.recompute_groups()
    after_comp = _components(state)

    # 受影响成员：撤销后所在连通分量发生变化的碎片（孤片恢复独立状态）
    affected = sorted(
        fid for fid in state.frags
        if before_comp[fid] != after_comp[fid])
    result = {
        "index": index,
        "action": "undo",
        "ok": True,
        "relation_id": key,
        "client_id": s.client_id,
        "affected_fragment_ids": affected,
        "groups": state.groups(),
    }
    return result, {"action": "undo", "key": key}


def _components(state: SimState) -> dict[int, frozenset[int]]:
    """当前 group_id 下各碎片所属分量的成员集合（孤片为单元素集合）。"""
    groups: dict[int, list[int]] = {}
    singles: list[int] = []
    for f in state.all_fragments():
        if f["group_id"] is None:
            singles.append(f["id"])
        else:
            groups.setdefault(f["group_id"], []).append(f["id"])
    out: dict[int, frozenset[int]] = {m: frozenset((m,)) for m in singles}
    for members in groups.values():
        comp = frozenset(members)
        for m in members:
            out[m] = comp
    return out


def simulate(d, steps) -> dict:
    """从数据库当前快照逐步推演操作序列（只读数据库，不写入）。

    返回（无论步骤是否成功）：
      ok=True  → steps / actions（供提交落库）/ assembly（最终快照）
      ok=False → steps（含失败步）/ failed_at / failure / assembly
                 （失败前一刻快照）
    仅请求级静态错误（空序列、重复 client_id 等）由 validate_steps 直接抛
    TrialError；步骤级问题（对象缺失、重复撤销、校验拒绝）一律装入 failure。
    """
    validate_steps(steps)
    state = SimState(d)
    results: list[dict] = []
    actions: list[dict] = []

    for i, s in enumerate(steps, start=1):
        try:
            if s.action == "relate":
                result, act = _sim_relate(state, s, i)
            else:
                result, act = _sim_undo(state, s, i)
        except TrialError as e:
            e.step = i
            e.action = s.action
            return {
                "ok": False,
                "base_version": state.base_version,
                "steps": results,
                "actions": actions,
                "failed_at": i,
                "failure": {
                    "step": i,
                    "action": s.action,
                    "code": e.code,
                    "message": e.message,
                    "conflicts": e.extra.get("conflicts", []),
                    "http_status": e.status,
                },
                "assembly": state.assembly(),
            }
        results.append(result)
        if act is not None:
            actions.append(act)
        if not result.get("ok", True):
            return {
                "ok": False,
                "base_version": state.base_version,
                "steps": results,
                "actions": actions,
                "failed_at": i,
                "failure": {
                    "step": i,
                    "action": "relate",
                    "code": "rejected",
                    "message": f"第 {i} 步建立关系未通过校验，序列在此中止",
                    "conflicts": result["conflicts"],
                    "http_status": 400,
                },
                "assembly": state.assembly(),
            }

    return {
        "ok": True,
        "base_version": state.base_version,
        "steps": results,
        "actions": actions,
        "failed_at": None,
        "failure": None,
        "assembly": state.assembly(),
    }


def commit_actions(d, sim: dict) -> dict[int, int]:
    """在当前事务内把预演过的动作序列落库。返回临时 key / client_id → 真实 id。"""
    key_to_rid: dict[int, int] = {}
    cid_to_rid: dict[str, int] = {}
    for act in sim["actions"]:
        if act["action"] == "relate":
            rid = d.insert_relation(
                act["fragment_a"], act["fragment_b"],
                act["markers_a"], act["markers_b"], act["max_error"])
            for fid, pose in act["new_poses"].items():
                d.update_pose(fid, pose)
            key_to_rid[act["key"]] = rid
            if act["client_id"] is not None:
                cid_to_rid[act["client_id"]] = rid
        else:
            key = act["key"]
            rid = key_to_rid.get(key, key)   # 负数=序列内新建，需映射
            d.undo_relation(rid)
    d.recompute_groups()
    return cid_to_rid
