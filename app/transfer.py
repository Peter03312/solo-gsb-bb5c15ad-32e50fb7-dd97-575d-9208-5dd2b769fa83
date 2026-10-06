"""离线工作站装配迁移：导出/导入带格式版本号的 JSON 包。

修复师更换离线工作站时，可把**当前装配及全部不可改版本快照**导出为一个
JSON 包，再在另一台**空白工作站**（仅允许存在空白基线、不得有用户碎片或
关系；该基线可由导入包替换）上导入：

- 导出 ``build_package``：包内带格式名与格式版本号、导出时间、当前装配
  （碎片原 ID / 轮廓 / 位姿 / 分组，关系含有效及已撤销状态、标记、误差、
  created_at / undone_at）与全部快照（原版本号、来源、快照时间、状态）。
- 导入先预览（只读、不写库）：``build_import_preview`` 整包校验并演算导入
  后的完整装配（按有效关系重算连通组），列出包内碎片、有效/已撤销关系、
  版本数与目标库状态。
- 确认（``commit_import``，由 main 在锁与单一事务内调用）：携带目标库
  当前装配版本，版本过期即拒绝且库与历史保持原状；通过后整体替换碎片、
  关系、快照与当前版本，原 ID / 轮廓 / 位姿 / 标记 / 状态 / 时间戳及各
  快照原版本号全部保留，不新增版本、不产生新快照。

整包拒绝（任一命中即拒绝，尽量一次性指出全部原因）：
格式版本不支持、包结构不合法、轮廓无效、关系引用缺失、快照版本重复、
快照状态版本不一致、快照元信息缺失、末版快照与包内当前装配不一致、
目标库非空白。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import geometry, history

# 迁移包格式：名称 + 语义版本号。解析端仅接受 SUPPORTED_FORMAT_VERSIONS。
FORMAT_NAME = "pottery-assembly-transfer"
FORMAT_VERSION = "1.0"
SUPPORTED_FORMAT_VERSIONS = {"1.0"}


class TransferError(Exception):
    """迁移包级错误（整次拒绝），status 为对应 HTTP 状态码。

    errors 为逐条原因：[{"field", "code", "message"}, ...]，field 指向
    迁移包中的位置（如 ``snapshots[2].state.fragments[0].contour``）。
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _point_list(v) -> bool:
    return (isinstance(v, list) and len(v) >= 2
            and all(isinstance(p, (list, tuple)) and len(p) >= 2
                    and _is_num(p[0]) and _is_num(p[1]) for p in v))


def _err(field: str, code: str, message: str) -> dict:
    return {"field": field, "code": code, "message": message}


# ---------------------------------------------------------------- 导出

def build_package(d) -> dict:
    """从库内读取当前装配与全部不可改快照，构建迁移包（只读）。

    快照按版本号升序排列；末版快照与当前装配在同一状态下读出，保证一致。
    """
    assembly = d.assembly_state()
    snapshots = d.all_snapshots()
    return {
        "format": {"name": FORMAT_NAME, "version": FORMAT_VERSION},
        "exported_at": _now(),
        "assembly": assembly,
        "snapshots": snapshots,
    }


# ---------------------------------------------------------------- 整包校验

def _check_state(state, prefix: str, errors: list[dict]) -> set[int]:
    """校验一个装配状态（当前装配或某快照 state）：碎片 id 唯一且轮廓有效、
    关系 id 唯一且引用的碎片在本状态内存在。

    全部问题追加进 errors，返回本状态内的碎片 id 集合。
    """
    if not isinstance(state, dict):
        errors.append(_err(prefix, "invalid_state", "装配状态必须是 JSON 对象"))
        return set()

    frag_ids: set[int] = set()
    frags = state.get("fragments")
    if not isinstance(frags, list):
        errors.append(_err(f"{prefix}.fragments", "invalid_fragments",
                           "fragments 必须是列表"))
    else:
        seen: set[int] = set()
        for i, f in enumerate(frags):
            fp = f"{prefix}.fragments[{i}]"
            if not isinstance(f, dict):
                errors.append(_err(fp, "invalid_fragment", "碎片必须是 JSON 对象"))
                continue
            fid = f.get("id")
            if not _is_int(fid) or fid < 1:
                errors.append(_err(f"{fp}.id", "invalid_fragment_id",
                                   "碎片 id 必须为正整数"))
            elif fid in seen:
                errors.append(_err(f"{fp}.id", "duplicate_fragment_id",
                                   f"碎片 id 重复：#{fid}"))
            else:
                seen.add(fid)
            try:
                geometry.validate_contour(f.get("contour"))
            except (geometry.GeometryError, TypeError, ValueError, KeyError) as e:
                errors.append(_err(f"{fp}.contour", "invalid_contour",
                                   f"碎片 #{fid if _is_int(fid) else i} 轮廓无效：{e}"))
            if not isinstance(f.get("name"), str) or not f["name"].strip():
                errors.append(_err(f"{fp}.name", "invalid_fragment_name",
                                   "碎片名称必须是非空字符串"))
            for axis in ("x", "y", "theta"):
                if not _is_num(f.get(axis)):
                    errors.append(_err(f"{fp}.{axis}", "invalid_pose",
                                       f"碎片 #{fid if _is_int(fid) else i} 的位姿"
                                       f" {axis} 必须是有限数值"))
            if "group_id" in f and f["group_id"] is not None and not _is_int(f["group_id"]):
                errors.append(_err(f"{fp}.group_id", "invalid_group_id",
                                   "group_id 必须为整数或 null"))
            if not isinstance(f.get("created_at"), str) or not f["created_at"]:
                errors.append(_err(f"{fp}.created_at", "invalid_created_at",
                                   "碎片 created_at 必须是原始时间戳字符串"))
        frag_ids = seen

    rels = state.get("relations")
    if not isinstance(rels, list):
        errors.append(_err(f"{prefix}.relations", "invalid_relations",
                           "relations 必须是列表"))
        return frag_ids

    seen_rel: set[int] = set()
    for i, r in enumerate(rels):
        rp = f"{prefix}.relations[{i}]"
        if not isinstance(r, dict):
            errors.append(_err(rp, "invalid_relation", "关系必须是 JSON 对象"))
            continue
        rid = r.get("id")
        if not _is_int(rid) or rid < 1:
            errors.append(_err(f"{rp}.id", "invalid_relation_id",
                               "关系 id 必须为正整数"))
        elif rid in seen_rel:
            errors.append(_err(f"{rp}.id", "duplicate_relation_id",
                               f"关系 id 重复：#{rid}"))
        else:
            seen_rel.add(rid)
        for side in ("fragment_a", "fragment_b"):
            ref = r.get(side)
            if not _is_int(ref) or ref < 1:
                errors.append(_err(f"{rp}.{side}", "relation_reference_missing",
                                   f"关系 #{rid if _is_int(rid) else i} 的 {side}"
                                   " 必须是有效的碎片 id"))
            elif ref not in frag_ids:
                errors.append(_err(
                    f"{rp}.{side}", "relation_reference_missing",
                    f"关系 #{rid if _is_int(rid) else i} 引用的碎片 #{ref}"
                    " 在该装配状态中不存在"))
        label = rid if _is_int(rid) else i
        for side in ("markers_a", "markers_b"):
            if not _point_list(r.get(side)):
                errors.append(_err(f"{rp}.{side}", "invalid_markers",
                                   f"关系 #{label} 的 {side} 必须是 ≥2 个坐标点的列表"))
        ma, mb = r.get("markers_a"), r.get("markers_b")
        if _point_list(ma) and _point_list(mb) and len(ma) != len(mb):
            errors.append(_err(rp, "marker_count_mismatch",
                               f"关系 #{label} 两侧标记数量不一致"
                               f"（{len(ma)} 对 {len(mb)}）"))
        if not _is_num(r.get("max_error")):
            errors.append(_err(f"{rp}.max_error", "invalid_max_error",
                               f"关系 #{label} 的 max_error 必须是有限数值"))
        if not isinstance(r.get("active"), bool):
            errors.append(_err(f"{rp}.active", "invalid_active",
                               f"关系 #{label} 的 active 必须是布尔值"))
        if not isinstance(r.get("created_at"), str) or not r["created_at"]:
            errors.append(_err(f"{rp}.created_at", "invalid_created_at",
                               f"关系 #{label} 的 created_at 必须是原始时间戳字符串"))
        if "undone_at" in r and r["undone_at"] is not None and \
                not isinstance(r["undone_at"], str):
            errors.append(_err(f"{rp}.undone_at", "invalid_undone_at",
                               f"关系 #{label} 的 undone_at 必须为字符串或 null"))
    return frag_ids


def validate_package(pkg) -> dict:
    """整包校验迁移包；任一不通过抛 TransferError(400 package_rejected)。

    通过后返回规范化内容 {"assembly": ..., "snapshots": [...]}（快照按
    版本升序）。格式名/格式版本无法识别时立即拒绝（无法信任其余内容）。
    """
    if not isinstance(pkg, dict):
        raise TransferError(400, "package_rejected",
                            "迁移包必须是 JSON 对象，整包拒绝",
                            [_err("", "invalid_package", "请求体不是 JSON 对象")])

    fmt = pkg.get("format")
    if not isinstance(fmt, dict) or fmt.get("name") != FORMAT_NAME:
        raise TransferError(
            400, "package_rejected",
            f"无法识别的迁移包：format.name 必须为 {FORMAT_NAME}，整包拒绝",
            [_err("format.name", "unsupported_format",
                  f"format.name 必须为 {FORMAT_NAME}")])
    fv = fmt.get("version")
    if not isinstance(fv, str) or fv not in SUPPORTED_FORMAT_VERSIONS:
        supported = "、".join(sorted(SUPPORTED_FORMAT_VERSIONS))
        raise TransferError(
            400, "package_rejected",
            f"不支持的迁移包格式版本 {fv!r}（本机支持：{supported}），整包拒绝",
            [_err("format.version", "format_version_unsupported",
                  f"格式版本 {fv!r} 不受支持，仅支持：{supported}", )])

    errors: list[dict] = []

    # ---- 当前装配
    assembly = pkg.get("assembly")
    asm_version = None
    if not isinstance(assembly, dict):
        errors.append(_err("assembly", "invalid_assembly",
                           "assembly 必须是 JSON 对象"))
    else:
        asm_version = assembly.get("version")
        if not _is_int(asm_version) or asm_version < 0:
            errors.append(_err("assembly.version", "invalid_assembly_version",
                               "当前装配版本必须是 ≥ 0 的整数"))
        _check_state(assembly, "assembly", errors)

    # ---- 快照
    snapshots = pkg.get("snapshots")
    parsed_snaps: list[dict] = []
    snap_versions: list[int] = []
    if not isinstance(snapshots, list) or not snapshots:
        errors.append(_err("snapshots", "empty_snapshots",
                           "snapshots 必须是非空列表（至少含末版快照）"))
    else:
        seen_versions: set[int] = set()
        for i, s in enumerate(snapshots):
            sp = f"snapshots[{i}]"
            if not isinstance(s, dict):
                errors.append(_err(sp, "invalid_snapshot", "快照必须是 JSON 对象"))
                continue
            sv = s.get("version")
            if not _is_int(sv) or sv < 0:
                errors.append(_err(f"{sp}.version", "invalid_snapshot_version",
                                   "快照版本必须是 ≥ 0 的整数"))
            else:
                if sv in seen_versions:
                    errors.append(_err(f"{sp}.version", "snapshot_version_duplicate",
                                       f"快照版本重复：v{sv}"))
                seen_versions.add(sv)
                snap_versions.append(sv)
            if not isinstance(s.get("source"), str) or not s["source"].strip():
                errors.append(_err(f"{sp}.source", "invalid_snapshot_source",
                                   "快照 source 必须是非空字符串"))
            if not isinstance(s.get("created_at"), str) or not s["created_at"]:
                errors.append(_err(f"{sp}.created_at", "invalid_snapshot_created_at",
                                   "快照 created_at 必须是原始快照时间戳字符串"))
            state = s.get("state")
            _check_state(state, f"{sp}.state", errors)
            if isinstance(state, dict) and _is_int(sv) and _is_int(state.get("version")):
                if state.get("version") != sv:
                    errors.append(_err(
                        sp, "snapshot_state_version_mismatch",
                        f"快照 v{sv} 的 state.version 为 {state.get('version')}，"
                        "两者必须一致"))
            parsed_snaps.append(s)

    # ---- 末版快照必须与包内当前装配一致
            # （版本号相同，且碎片/关系逐条一致；连通组由有效关系重算）
    if isinstance(assembly, dict) and _is_int(asm_version) and snap_versions:
        last_version = max(snap_versions)
        last_snap = next(
            (s for s in parsed_snaps
             if _is_int(s.get("version")) and s["version"] == last_version), None)
        if last_version != asm_version:
            errors.append(_err(
                "snapshots", "last_snapshot_mismatch",
                f"末版快照为 v{last_version}，与包内当前装配 v{asm_version} 不一致"))
        elif isinstance(last_snap, dict) and isinstance(last_snap.get("state"), dict):
            diffs = _state_diff(last_snap["state"], assembly)
            if diffs:
                errors.append(_err(
                    "snapshots", "last_snapshot_mismatch",
                    "末版快照内容与包内当前装配不一致：" + "；".join(diffs)))

    if errors:
        raise TransferError(
            400, "package_rejected",
            f"迁移包校验未通过（{len(errors)} 处问题），整包拒绝，未写入任何数据",
            errors=errors)
    return {"format_version": fv, "exported_at": pkg.get("exported_at"),
            "assembly": assembly,
            "snapshots": sorted(parsed_snaps, key=lambda s: s["version"])}


def _canon(objects) -> dict[int, str]:
    """把碎片/关系列表按 id 规整为可比较的规范化 JSON 字符串映射。"""
    out = {}
    for o in objects:
        try:
            out[int(o["id"])] = json.dumps(o, sort_keys=True, ensure_ascii=False)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _state_diff(a: dict, b: dict) -> list[str]:
    """对比末版快照状态 a 与当前装配 b 的碎片、关系差异（返回人类可读摘要）。"""
    diffs: list[str] = []
    af, bf = _canon(a.get("fragments", [])), _canon(b.get("fragments", []))
    ar, br = _canon(a.get("relations", [])), _canon(b.get("relations", []))
    only_a = sorted(set(af) - set(bf))
    only_b = sorted(set(bf) - set(af))
    changed_f = sorted(i for i in set(af) & set(bf) if af[i] != bf[i])
    if only_a:
        diffs.append(f"仅末版快照存在的碎片 {only_a}")
    if only_b:
        diffs.append(f"仅当前装配存在的碎片 {only_b}")
    if changed_f:
        diffs.append(f"碎片内容不一致 {changed_f}")
    only_ar = sorted(set(ar) - set(br))
    only_br = sorted(set(br) - set(ar))
    changed_r = sorted(i for i in set(ar) & set(br) if ar[i] != br[i])
    if only_ar:
        diffs.append(f"仅末版快照存在的关系 {only_ar}")
    if only_br:
        diffs.append(f"仅当前装配存在的关系 {only_br}")
    if changed_r:
        diffs.append(f"关系内容不一致 {changed_r}")
    return diffs


# ---------------------------------------------------------------- 导入预览演算

def _recomputed_assembly(assembly: dict) -> dict:
    """按包内有效关系重算连通组后的完整装配（预览所见即导入所得）。"""
    fragments = [dict(f) for f in assembly["fragments"]]
    ids = {f["id"] for f in fragments}
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for r in assembly["relations"]:
        if r.get("active") and r["fragment_a"] in parent and r["fragment_b"] in parent:
            union(r["fragment_a"], r["fragment_b"])

    comps: dict[int, list[int]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    groups = []
    for members in comps.values():
        gid = min(members) if len(members) > 1 else None
        for m in members:
            next(f for f in fragments if f["id"] == m)["group_id"] = gid
        if gid is not None:
            groups.append({"id": gid, "fragment_ids": sorted(members)})
    groups.sort(key=lambda g: g["id"])
    fragments.sort(key=lambda f: f["id"])
    return {"version": assembly["version"], "fragments": fragments,
            "groups": groups,
            "relations": sorted(assembly["relations"], key=lambda r: r["id"])}


def check_target_blank(d):
    """目标库仅允许空白基线：不得有任何用户碎片或关系。"""
    frag_count = len(d.all_fragments())
    rel_count = len(d.all_relations())
    if frag_count or rel_count:
        raise TransferError(
            400, "target_not_blank",
            f"目标库不是空白工作站：存在 {frag_count} 片碎片、{rel_count} 条关系，"
            "仅允许存在空白基线，整包拒绝且不写入",
            extra={"target": {"current_version": d.version(), "blank": False,
                              "fragment_count": frag_count,
                              "relation_count": rel_count}})


def build_import_preview(d, pkg) -> dict:
    """导入预览：整包校验 + 目标库空白校验 + 演算导入后装配（只读不写库）。"""
    validated = validate_package(pkg)
    check_target_blank(d)
    return _preview_body(d, validated)


def commit_import(d, expected_version: int, pkg) -> dict:
    """确认导入：在调用方持有的锁与单一事务内执行整体替换。

    - 先用同一包重新整包校验并确认目标库仍为空白（任何问题整包拒绝）；
    - 目标库当前装配版本必须与 expected_version 一致，否则 409 拒绝，
      库与历史保持原状；
    - 通过后替换碎片与关系（原 ID、轮廓、位姿、标记、状态、时间戳原样），
      按有效关系重算连通组，再整体替换快照表（各快照原版本号/来源/时间戳
      原样），当前版本沿用包内当前装配版本号——不新增版本、不产生新快照。
    """
    validated = validate_package(pkg)
    check_target_blank(d)
    current = d.version()
    if expected_version != current:
        raise TransferError(
            409, "version_conflict",
            f"版本冲突：目标库当前装配版本为 v{current}，"
            f"导入预览依据的 v{expected_version} 已过期，整包拒绝，库与历史保持原状",
            extra={"current_version": current,
                   "expected_version": expected_version})

    assembly = validated["assembly"]
    d.restore_state(assembly)
    d.recompute_groups()
    d.replace_snapshots(validated["snapshots"])
    d.set_version(assembly["version"])
    imported = d.assembly_state()
    return {
        "ok": True,
        "imported_version": assembly["version"],
        "expected_version": expected_version,
        "snapshot_count": len(validated["snapshots"]),
        "fragment_count": len(imported["fragments"]),
        "relation_count": len(imported["relations"]),
        "assembly": imported,
    }


def _preview_body(d, validated: dict) -> dict:
    assembly = validated["assembly"]
    snapshots = validated["snapshots"]
    active = [r for r in assembly["relations"] if r.get("active")]
    undone = [r for r in assembly["relations"] if not r.get("active")]
    return {
        "ok": True,
        "format_version": validated["format_version"],
        "exported_at": validated["exported_at"],
        "package": {
            "current_version": assembly["version"],
            "fragment_count": len(assembly["fragments"]),
            "relation_count": len(assembly["relations"]),
            "active_relation_count": len(active),
            "undone_relation_count": len(undone),
            "snapshot_count": len(snapshots),
            "versions": [{"version": s["version"], "source": s["source"],
                          "source_label": history.source_label(s["source"]),
                          "created_at": s["created_at"]} for s in snapshots],
            "fragments": [{"id": f["id"], "name": f.get("name", ""),
                           "contour_vertices": len(f.get("contour", []))}
                          for f in sorted(assembly["fragments"], key=lambda f: f["id"])],
            "relations": [{"id": r["id"], "fragment_a": r["fragment_a"],
                           "fragment_b": r["fragment_b"],
                           "active": bool(r.get("active")),
                           "created_at": r.get("created_at"),
                           "undone_at": r.get("undone_at")}
                          for r in sorted(assembly["relations"], key=lambda r: r["id"])],
        },
        "target": {"current_version": d.version(), "blank": True,
                   "fragment_count": 0, "relation_count": 0},
        "imported_assembly": _recomputed_assembly(assembly),
    }
