"""离线工作站迁移：装配包导出 / 校验 / 预览演算（只读）。

修复师更换离线工作站时，可把**当前装配及全部不可改版本快照**导出为带格式
版本号的 JSON 包，再导入另一台空白工作站。本模块只负责：

- ``build_package(d)``             从库内导出 JSON 包（当前装配 + 全部快照）；
- ``validate_package(pkg)``        整包校验：格式版本、结构、轮廓、关系引用、
                                   快照版本重复 / 连续性、末版与包内当前装配
                                   是否一致；不通过抛 ``PackageError``；
- ``build_import_preview(...)``    整理导入预览（包内碎片、有效及已撤销关系、
                                   版本数、导入后装配），不写库。

确认导入的单事务写入（保留碎片 / 关系原 ID、轮廓、位姿、标记、状态、时间戳
及各快照原版本号，空白基线被整包替换）由 main.py 在锁与事务内调用
``DB.import_package`` 完成（见 database.py）。
"""
from __future__ import annotations

from . import geometry

PACKAGE_FORMAT = "pottery-assembly"
SUPPORTED_FORMAT_VERSIONS = ("1.0",)

# 包内碎片必须具备的字段（关系 / 快照同理）；多余字段忽略，缺失即拒绝
_FRAG_FIELDS = ("id", "name", "contour", "x", "y", "theta", "group_id",
                "created_at")
_REL_FIELDS = ("id", "fragment_a", "fragment_b", "markers_a", "markers_b",
               "max_error", "active", "created_at", "undone_at")


class PackageError(Exception):
    """迁移包错误（整次拒绝），status 为对应 HTTP 状态码。

    reasons 为全部拒绝原因（可多条）；code 为主原因代码，供程序化判断。
    """

    def __init__(self, status: int, code: str, message: str,
                 reasons: list[str] | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.reasons = reasons or [message]
        self.extra = extra or {}

    def detail(self) -> dict:
        d = {"message": self.message, "code": self.code,
             "reasons": self.reasons}
        d.update(self.extra)
        return d


def _reject(code: str, reasons: list[str], status: int = 400):
    raise PackageError(status, code, "迁移包整次拒绝：" + "；".join(reasons),
                       reasons=reasons)


# ---------------------------------------------------------------- 导出

def build_package(d, exported_at: str) -> dict:
    """导出当前装配与全部不可改快照为 JSON 可序列化的包。

    快照按版本升序排列；current_assembly 直接取库内当前装配状态，与末版
    快照内容一致（每次成功变更都同事务固化快照）。
    """
    rows = d.conn.execute(
        "SELECT version, source, state, created_at FROM snapshots"
        " ORDER BY version ASC").fetchall()
    snapshots = [{
        "version": r["version"],
        "source": r["source"],
        "created_at": r["created_at"],
        "state": _json(r["state"]),
    } for r in rows]
    return {
        "format": PACKAGE_FORMAT,
        "format_version": SUPPORTED_FORMAT_VERSIONS[0],
        "exported_at": exported_at,
        "current_version": d.version(),
        "current_assembly": d.assembly_state(),
        "snapshots": snapshots,
    }


def _json(value):
    import json
    return json.loads(value)


# ---------------------------------------------------------------- 基础类型

def _is_num(v) -> bool:
    # bool 是 int 的子类，须显式排除
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_point(v) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and _is_num(v[0]) and _is_num(v[1]))


def _is_contour(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) >= 3 and all(_is_point(p) for p in v)


def _norm_contour(v) -> list[list[float]]:
    return [[float(p[0]), float(p[1])] for p in v]


def _norm_markers(v) -> list[list[float]]:
    return [[float(p[0]), float(p[1])] for p in v]


# ---------------------------------------------------------------- 整包校验

def validate_package(pkg) -> dict:
    """整包校验并返回规范化后的包；任一问题即整次拒绝（PackageError 400）。

    校验项（全部问题一次性收集后拒绝）：
    - 顶层结构 / 格式标识 / 格式版本（不支持 → unsupported_format_version）；
    - 碎片：字段齐全、id 正整数不重复、轮廓为有效非自交闭合轮廓、
      位姿 / 时间戳 / 名称合法；
    - 关系：字段齐全、id 正整数不重复、两侧引用的碎片存在、标记成对；
    - 快照：版本非负整数、不重复、自 v0/v1 起连续无缺号、state 结构完整、
      快照内关系引用不缺失、快照内轮廓有效、state.version 与快照版本一致；
    - 末版快照内容必须与包内 current_assembly 一致（否则拒绝）。
    """
    reasons: list[str] = []
    primary = {"code": "invalid_package"}

    def fail(code: str, why: str):
        reasons.append(why)
        if primary["code"] == "invalid_package":
            primary["code"] = code

    if not isinstance(pkg, dict):
        _reject("invalid_package", ["迁移包必须是 JSON 对象"])

    # ---- 格式标识与格式版本
    if pkg.get("format") != PACKAGE_FORMAT:
        fail("invalid_format", f"格式标识不正确（应为 {PACKAGE_FORMAT}）")
    fv = pkg.get("format_version")
    if not isinstance(fv, str):
        fail("unsupported_format_version", "缺少字符串类型的 format_version")
    elif fv not in SUPPORTED_FORMAT_VERSIONS:
        _reject("unsupported_format_version",
                [f"不支持的格式版本 {fv!r}，本工作站支持："
                 f"{', '.join(SUPPORTED_FORMAT_VERSIONS)}"])
    cur_v = pkg.get("current_version")
    if not isinstance(cur_v, int) or isinstance(cur_v, bool) or cur_v < 0:
        fail("invalid_current_version", "current_version 必须为非负整数")
        cur_v = None
    cur_asm = pkg.get("current_assembly")
    if not isinstance(cur_asm, dict):
        fail("invalid_current_assembly", "缺少 current_assembly（当前装配）对象")

    # ---- 碎片
    frags_raw = cur_asm.get("fragments") if isinstance(cur_asm, dict) else None
    frags, frag_ids = [], set()
    if not isinstance(frags_raw, list):
        fail("invalid_fragments", "current_assembly.fragments 必须为列表")
    else:
        for i, f in enumerate(frags_raw):
            where = f"current_assembly.fragments[{i}]"
            if not isinstance(f, dict):
                fail("invalid_fragment", f"{where} 必须为对象")
                continue
            missing = [k for k in _FRAG_FIELDS if k not in f]
            if missing:
                fail("invalid_fragment",
                     f"{where}（碎片 #{f.get('id')}）缺少字段：{', '.join(missing)}")
                continue
            fid = f["id"]
            if not isinstance(fid, int) or isinstance(fid, bool) or fid < 1:
                fail("invalid_fragment_id", f"{where} 的 id 必须为正整数")
                continue
            if fid in frag_ids:
                fail("duplicate_fragment_id", f"碎片 id 重复：#{fid}")
            frag_ids.add(fid)
            if not isinstance(f["name"], str) or not f["name"].strip():
                fail("invalid_fragment", f"碎片 #{fid} 的名称不能为空")
            if not _is_contour(f["contour"]):
                fail("invalid_contour",
                     f"碎片 #{fid} 的轮廓无效（至少 3 个 [x, y] 顶点）")
            elif not _contour_ok(f["contour"], f"碎片 #{fid}", reasons, fail):
                pass
            for k in ("x", "y", "theta"):
                if not _is_num(f[k]):
                    fail("invalid_pose", f"碎片 #{fid} 的位姿字段 {k} 必须为数字")
            if f["group_id"] is not None and (
                    not isinstance(f["group_id"], int)
                    or isinstance(f["group_id"], bool)):
                fail("invalid_group_id", f"碎片 #{fid} 的 group_id 必须为整数或 null")
            if not isinstance(f["created_at"], str) or not f["created_at"]:
                fail("invalid_timestamp", f"碎片 #{fid} 的 created_at 必须为非空字符串")
            if _is_contour(f.get("contour")):
                frags.append(_norm_fragment(f))

    # ---- 关系
    rels_raw = cur_asm.get("relations") if isinstance(cur_asm, dict) else None
    rels, rel_ids = [], set()
    if not isinstance(rels_raw, list):
        fail("invalid_relations", "current_assembly.relations 必须为列表")
    else:
        for i, r in enumerate(rels_raw):
            where = f"current_assembly.relations[{i}]"
            if not isinstance(r, dict):
                fail("invalid_relation", f"{where} 必须为对象")
                continue
            missing = [k for k in _REL_FIELDS if k not in r]
            if missing:
                fail("invalid_relation",
                     f"{where}（关系 #{r.get('id')}）缺少字段：{', '.join(missing)}")
                continue
            rid = r["id"]
            if not isinstance(rid, int) or isinstance(rid, bool) or rid < 1:
                fail("invalid_relation_id", f"{where} 的 id 必须为正整数")
                continue
            if rid in rel_ids:
                fail("duplicate_relation_id", f"关系 id 重复：#{rid}")
            rel_ids.add(rid)
            _validate_relation_body(r, where, frag_ids, reasons, fail)
            rels.append(_norm_relation(r))

    # ---- 快照
    snaps_raw = pkg.get("snapshots")
    norm_snaps: list[dict] = []
    snap_versions: set[int] = set()
    if not isinstance(snaps_raw, list) or not snaps_raw:
        fail("invalid_snapshots", "snapshots 必须为非空列表（至少包含基线快照）")
    else:
        for i, s in enumerate(snaps_raw):
            where = f"snapshots[{i}]"
            if not isinstance(s, dict):
                fail("invalid_snapshot", f"{where} 必须为对象")
                continue
            v = s.get("version")
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                fail("invalid_snapshot_version",
                     f"{where} 的 version 必须为非负整数")
                continue
            if v in snap_versions:
                fail("duplicate_snapshot_version",
                     f"快照版本重复：v{v}（各快照原版本号不得重复）")
            snap_versions.add(v)
            if not isinstance(s.get("source"), str) or not s["source"]:
                fail("invalid_snapshot", f"快照 v{v} 缺少非空 source")
            if not isinstance(s.get("created_at"), str) or not s["created_at"]:
                fail("invalid_snapshot", f"快照 v{v} 缺少非空 created_at")
            st = s.get("state")
            if not isinstance(st, dict):
                fail("invalid_snapshot_state", f"快照 v{v} 缺少 state 对象")
                continue
            if st.get("version") != v:
                fail("snapshot_state_version_mismatch",
                     f"快照 v{v} 的 state.version={st.get('version')} 与快照版本号不一致")
            _validate_snapshot_state(st, v, reasons, fail)
            norm_snaps.append({"version": v, "source": s["source"],
                               "created_at": s["created_at"],
                               "state": _norm_state(st)})

        # 版本必须自最小版本（0 或 1）起连续无缺号
        if snap_versions:
            lo = min(snap_versions)
            if lo not in (0, 1):
                fail("invalid_snapshot_versions",
                     f"最早快照版本必须为 v0 或 v1（实际 v{lo}）")
            missing_vs = [v for v in range(lo, max(snap_versions) + 1)
                          if v not in snap_versions]
            if missing_vs:
                fail("snapshot_version_gap",
                     "快照版本不连续，缺少版本："
                     + ", ".join(f"v{v}" for v in missing_vs))

    if reasons:
        _reject(primary["code"], reasons)

    # ---- current_version 与快照末版一致
    max_snap = max(snap_versions)
    if cur_v is not None and cur_v != max_snap:
        _reject("current_version_snapshot_mismatch",
                [f"current_version=v{cur_v} 与包内末版快照 v{max_snap} 不一致"])

    # ---- 末版快照与包内当前装配逐对象一致（轮廓 / 位姿 / 标记 / 状态 / 时间戳）
    last_state = next(s["state"] for s in norm_snaps if s["version"] == max_snap)
    diffs = _states_diff(last_state, _norm_state(cur_asm))
    if diffs:
        _reject("current_assembly_mismatch",
                ["末版快照与包内当前装配不一致，整包拒绝：" + "；".join(diffs)])

    return {
        "format": PACKAGE_FORMAT,
        "format_version": fv,
        "exported_at": pkg.get("exported_at"),
        "current_version": cur_v,
        "current_assembly": _norm_state(cur_asm),
        "snapshots": sorted(norm_snaps, key=lambda s: s["version"]),
    }


def _contour_ok(contour, label: str, reasons, fail) -> bool:
    """对轮廓跑既有非自交校验；失败登记原因。"""
    try:
        geometry.validate_contour(contour)
    except geometry.GeometryError as e:
        fail("invalid_contour", f"{label} 轮廓无效：{e}")
        return False
    return True


def _validate_relation_body(r: dict, where: str, frag_ids: set,
                            reasons: list, fail):
    rid = r["id"]
    for side in ("a", "b"):
        fid = r[f"fragment_{side}"]
        if not isinstance(fid, int) or isinstance(fid, bool) or fid < 1:
            fail("invalid_relation", f"关系 #{rid} 的 fragment_{side} 必须为正整数")
        elif fid not in frag_ids:
            fail("relation_reference_missing",
                 f"关系 #{rid} 引用的碎片 #{fid} 在包内不存在（{where}）")
    if (isinstance(r["fragment_a"], int) and isinstance(r["fragment_b"], int)
            and r["fragment_a"] == r["fragment_b"]):
        fail("invalid_relation", f"关系 #{rid} 的两片碎片不能相同")
    ma, mb = r.get("markers_a"), r.get("markers_b")
    for name, ms in (("markers_a", ma), ("markers_b", mb)):
        if not isinstance(ms, list) or not all(_is_point(p) for p in ms):
            fail("invalid_markers",
                 f"关系 #{rid} 的 {name} 必须为 [x, y] 点列表")
    if isinstance(ma, list) and isinstance(mb, list) and len(ma) != len(mb):
        fail("invalid_markers",
             f"关系 #{rid} 两侧标记数量不一致（{len(ma)} 对 {len(mb)}）")
    if not _is_num(r["max_error"]):
        fail("invalid_relation", f"关系 #{rid} 的 max_error 必须为数字")
    if not isinstance(r["active"], bool):
        fail("invalid_relation", f"关系 #{rid} 的 active 必须为布尔值")
    if not isinstance(r["created_at"], str) or not r["created_at"]:
        fail("invalid_timestamp", f"关系 #{rid} 的 created_at 必须为非空字符串")
    if r["undone_at"] is not None and not isinstance(r["undone_at"], str):
        fail("invalid_timestamp", f"关系 #{rid} 的 undone_at 必须为字符串或 null")


def _validate_snapshot_state(st: dict, v: int, reasons: list, fail):
    """校验单个快照 state 内部一致性（结构、引用、轮廓）。"""
    frags = st.get("fragments")
    rels = st.get("relations")
    if not isinstance(frags, list):
        fail("invalid_snapshot_state", f"快照 v{v} 的 state.fragments 必须为列表")
        frags = []
    if not isinstance(rels, list):
        fail("invalid_snapshot_state", f"快照 v{v} 的 state.relations 必须为列表")
        rels = []
    fids: set[int] = set()
    for i, f in enumerate(frags):
        if not isinstance(f, dict) or "id" not in f:
            fail("invalid_snapshot_state", f"快照 v{v} 的 fragments[{i}] 结构不完整")
            continue
        fid = f["id"]
        if not isinstance(fid, int) or isinstance(fid, bool) or fid < 1:
            fail("invalid_snapshot_state", f"快照 v{v} 存在非法碎片 id：{fid!r}")
            continue
        if fid in fids:
            fail("duplicate_fragment_id", f"快照 v{v} 内碎片 id 重复：#{fid}")
        fids.add(fid)
        missing = [k for k in _FRAG_FIELDS if k not in f]
        if missing:
            fail("invalid_snapshot_state",
                 f"快照 v{v} 的碎片 #{fid} 缺少字段：{', '.join(missing)}")
            continue
        if _is_contour(f.get("contour")):
            _contour_ok(f["contour"], f"快照 v{v} 碎片 #{fid}", reasons, fail)
    for i, r in enumerate(rels):
        if not isinstance(r, dict) or "id" not in r:
            fail("invalid_snapshot_state", f"快照 v{v} 的 relations[{i}] 结构不完整")
            continue
        rid = r["id"]
        missing = [k for k in _REL_FIELDS if k not in r]
        if missing:
            fail("invalid_snapshot_state",
                 f"快照 v{v} 的关系 #{rid} 缺少字段：{', '.join(missing)}")
            continue
        for side in ("a", "b"):
            fid = r[f"fragment_{side}"]
            if fid not in fids:
                fail("relation_reference_missing",
                     f"快照 v{v} 的关系 #{rid} 引用缺失碎片 #{fid}")
        for name in ("markers_a", "markers_b"):
            if not isinstance(r[name], list) or not all(_is_point(p) for p in r[name]):
                fail("invalid_markers",
                     f"快照 v{v} 的关系 #{rid} 的 {name} 不是有效点列表")
        if not isinstance(r["active"], bool):
            fail("invalid_snapshot_state",
                 f"快照 v{v} 的关系 #{rid} 的 active 必须为布尔值")


# ---------------------------------------------------------------- 规范化

def _norm_fragment(f: dict) -> dict:
    return {
        "id": int(f["id"]),
        "name": f["name"],
        "contour": _norm_contour(f["contour"]),
        "x": float(f["x"]),
        "y": float(f["y"]),
        "theta": float(f["theta"]),
        "group_id": (int(f["group_id"]) if f["group_id"] is not None else None),
        "created_at": f["created_at"],
    }


def _norm_relation(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "fragment_a": int(r["fragment_a"]),
        "fragment_b": int(r["fragment_b"]),
        "markers_a": _norm_markers(r["markers_a"]),
        "markers_b": _norm_markers(r["markers_b"]),
        "max_error": float(r["max_error"]),
        "active": bool(r["active"]),
        "created_at": r["created_at"],
        "undone_at": r["undone_at"],
    }


def _norm_state(st: dict) -> dict:
    """规范化装配 state：碎片 / 关系按 id 排序，坐标全部转 float 后再比较。"""
    return {
        "version": st["version"],
        "fragments": sorted((_norm_fragment(f) for f in st["fragments"]),
                            key=lambda f: f["id"]),
        "groups": st.get("groups", []),
        "relations": sorted((_norm_relation(r) for r in st["relations"]),
                            key=lambda r: r["id"]),
    }


def _states_diff(a: dict, b: dict) -> list[str]:
    """末版快照与当前装配逐对象比对，返回差异描述（空列表表示一致）。"""
    out: list[str] = []
    fa = {f["id"]: f for f in a["fragments"]}
    fb = {f["id"]: f for f in b["fragments"]}
    for fid in sorted(set(fa) | set(fb)):
        if fid not in fa:
            out.append(f"当前装配缺少末版快照中的碎片 #{fid}")
        elif fid not in fb:
            out.append(f"当前装配多出碎片 #{fid}")
        elif fa[fid] != fb[fid]:
            changed = [k for k in _FRAG_FIELDS if fa[fid].get(k) != fb[fid].get(k)]
            out.append(f"碎片 #{fid} 字段不一致：{', '.join(changed)}")
    ra = {r["id"]: r for r in a["relations"]}
    rb = {r["id"]: r for r in b["relations"]}
    for rid in sorted(set(ra) | set(rb)):
        if rid not in ra:
            out.append(f"当前装配缺少末版快照中的关系 #{rid}")
        elif rid not in rb:
            out.append(f"当前装配多出关系 #{rid}")
        elif ra[rid] != rb[rid]:
            changed = [k for k in _REL_FIELDS if ra[rid].get(k) != rb[rid].get(k)]
            out.append(f"关系 #{rid} 字段不一致：{', '.join(changed)}")
    return out


# ---------------------------------------------------------------- 预览

def target_is_blank(d) -> tuple[bool, dict]:
    """目标库是否为「仅空白基线」状态：无任何用户碎片或关系。

    基线快照内容也必须为空（非空说明曾有用户数据，即便现已删空也不算
    空白工作站）。返回 (是否空白, 现状摘要)。
    """
    frags = d.all_fragments()
    rels = d.all_relations()
    versions = d.list_snapshot_versions()
    blank_snap = True
    nonempty_snaps: list[int] = []
    for it in versions:
        st = d.get_snapshot_state(it["version"])
        if st and (st.get("fragments") or st.get("relations")):
            blank_snap = False
            nonempty_snaps.append(it["version"])
    is_blank = not frags and not rels and blank_snap
    return is_blank, {
        "current_version": d.version(),
        "fragment_count": len(frags),
        "relation_count": len(rels),
        "snapshot_count": len(versions),
        "nonempty_snapshot_versions": nonempty_snaps,
    }


def groups_of_state(state: dict) -> list[dict]:
    """按 state 中有效关系重算导入后连通组（与 DB.recompute_groups 同语义）。"""
    ids = {int(f["id"]) for f in state["fragments"]}
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in state["relations"]:
        if r["active"] and r["fragment_a"] in parent and r["fragment_b"] in parent:
            ra, rb = find(r["fragment_a"]), find(r["fragment_b"])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    comps: dict[int, list[int]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    groups = []
    for members in comps.values():
        if len(members) > 1:
            groups.append({"id": min(members), "fragment_ids": sorted(members)})
    return sorted(groups, key=lambda g: g["id"])


def build_import_preview(pkg: dict, target: dict) -> dict:
    """整理导入预览响应（不写库）。

    pkg 为 validate_package 通过后的规范化包；target 为 target_is_blank
    返回的目标库现状摘要。ok=false 表示目标库非空白，确认应被拒绝。
    """
    asm = pkg["current_assembly"]
    active = [r for r in asm["relations"] if r["active"]]
    undone = [r for r in asm["relations"] if not r["active"]]
    groups = groups_of_state(asm)
    imported_assembly = {
        "version": pkg["current_version"],
        "historical": False,
        "fragments": asm["fragments"],
        "groups": groups,
        "relations": asm["relations"],
    }
    is_blank = target["fragment_count"] == 0 and target["relation_count"] == 0 \
        and not target["nonempty_snapshot_versions"]
    return {
        "ok": is_blank,
        "package": {
            "format": pkg["format"],
            "format_version": pkg["format_version"],
            "exported_at": pkg["exported_at"],
            "current_version": pkg["current_version"],
            "snapshot_count": len(pkg["snapshots"]),
            "versions": [{"version": s["version"], "source": s["source"],
                          "created_at": s["created_at"]}
                         for s in pkg["snapshots"]],
        },
        "target": target,
        "fragment_count": len(asm["fragments"]),
        "active_relation_count": len(active),
        "undone_relation_count": len(undone),
        "active_relations": active,
        "undone_relations": undone,
        "fragments": asm["fragments"],
        "imported_assembly": imported_assembly,
        "imported_version": pkg["current_version"],
        "reasons": [] if is_blank else [
            "目标库不是空白工作站：仅允许存在无碎片、无关系的空白基线，"
            "该基线可由导入包替换"],
    }
