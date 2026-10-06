"""装配历史版本：查看、差异演算与恢复。

不可改的版本快照存于 snapshots 表（见 database.py）。本模块只做**只读**
演算与响应整理：

- ``groups_of(state)``         从快照/当前状态的碎片重算连通组；
- ``diff_assembly(current, target)``  对比当前版与目标历史版的碎片、关系
  （含有效/已撤销状态）差异；
- ``build_history_view(...)``  整理「查看历史版」「恢复预览」响应体。

恢复的原子写入（原 ID、原状态、重算连通组、版本只递增一次、留新快照）
在 main.py 的锁与事务内通过 DB.restore_state 完成。
"""
from __future__ import annotations

# 快照来源代码 → 人类可读说明（未知来源沿用原始 code）
SOURCE_LABELS = {
    "baseline": "基线（启用版本历史时记录）",
    "create": "录入碎片",
    "delete": "删除碎片",
    "accept": "采纳拼接",
    "undo": "撤销拼接",
    "trial": "有序试拼确认",
    "revision": "轮廓修订确认",
    "calibration": "锚点整体校准",
    "restore": "版本恢复",
}


def source_label(code: str) -> str:
    return SOURCE_LABELS.get(code, code)


def groups_of(fragments) -> list[dict]:
    """按碎片的 group_id 汇总连通组（结构与装配快照中的 groups 一致）。"""
    groups: dict[int, list[int]] = {}
    for f in fragments:
        if f["group_id"] is not None:
            groups.setdefault(f["group_id"], []).append(f["id"])
    return [{"id": gid, "fragment_ids": sorted(ids)}
            for gid, ids in sorted(groups.items())]


def target_assembly(snap: dict) -> dict:
    """历史快照 → 以目标版本号呈现的完整装配（只读）。

    连通组按快照碎片重算（快照生成时即与库内重算结果一致），不依赖
    快照里存的 groups，保证恢复预览所见即恢复后所得。
    """
    fragments = sorted(snap["fragments"], key=lambda f: f["id"])
    return {
        "version": snap["version"],
        "historical": True,
        "fragments": fragments,
        "groups": groups_of(fragments),
        "relations": sorted(snap["relations"], key=lambda r: r["id"]),
    }


# ---------------------------------------------------------------- 差异

# 这些字段一致即视为「无变化」；group_id 不参与（连通组单独汇总）
_FRAG_FIELDS = ("name", "contour", "x", "y", "theta")
_REL_FIELDS = ("fragment_a", "fragment_b", "markers_a", "markers_b",
               "max_error", "active", "created_at", "undone_at")


def _same(a, b, fields) -> bool:
    return all(a[k] == b[k] for k in fields)


def diff_assembly(current: dict, target: dict) -> dict:
    """对比当前版 current 与目标历史版 target 的碎片、关系差异。

    碎片按 id、关系按 id 配对：
    - 仅目标有 → restored（恢复后重现，含曾被删除的碎片/关系）；
    - 仅当前有 → removed（恢复后消失，含曾被删除的关系）；
    - 两边都有但内容不同 → changed；两边一致 → unchanged。
    另给 groups 摘要（当前/目标）与各分类计数。
    """
    cf = {int(f["id"]): f for f in current["fragments"]}
    tf = {int(f["id"]): f for f in target["fragments"]}
    cr = {int(r["id"]): r for r in current["relations"]}
    tr = {int(r["id"]): r for r in target["relations"]}

    frag_changed, frag_unchanged, frag_restored, frag_removed = [], [], [], []
    for fid in sorted(set(cf) | set(tf)):
        a, b = cf.get(fid), tf.get(fid)
        if a is not None and b is None:
            item = {"id": fid, "current": a}
            frag_removed.append(item)
        elif a is None and b is not None:
            item = {"id": fid, "target": b}
            frag_restored.append(item)
        elif not _same(a, b, _FRAG_FIELDS) or a.get("group_id") != b.get("group_id"):
            changed_fields = [k for k in (*_FRAG_FIELDS, "group_id") if a[k] != b[k]]
            frag_changed.append({"id": fid, "changed_fields": changed_fields,
                                 "current": a, "target": b})
        else:
            frag_unchanged.append({"id": fid})

    rel_changed, rel_unchanged, rel_restored, rel_removed = [], [], [], []
    for rid in sorted(set(cr) | set(tr)):
        a, b = cr.get(rid), tr.get(rid)
        if a is not None and b is None:
            rel_removed.append({"id": rid, "current": a})
        elif a is None and b is not None:
            rel_restored.append({"id": rid, "target": b})
        elif not _same(a, b, _REL_FIELDS):
            changed_fields = [k for k in _REL_FIELDS if a[k] != b[k]]
            rel_changed.append({"id": rid, "changed_fields": changed_fields,
                                "current": a, "target": b})
        else:
            rel_unchanged.append({"id": rid, "active": a["active"]})

    return {
        "current_version": current["version"],
        "target_version": target["version"],
        "fragments": {
            "restored": frag_restored,
            "removed": frag_removed,
            "changed": frag_changed,
            "unchanged": frag_unchanged,
        },
        "relations": {
            "restored": rel_restored,
            "removed": rel_removed,
            "changed": rel_changed,
            "unchanged": rel_unchanged,
        },
        "groups": {"current": current.get("groups", groups_of(
                                current.get("fragments", []))),
                   "target": target.get("groups", groups_of(
                                target.get("fragments", [])))},
        "summary": {
            "fragments": {
                "restored": len(frag_restored),
                "removed": len(frag_removed),
                "changed": len(frag_changed),
                "unchanged": len(frag_unchanged),
            },
            "relations": {
                "restored": len(rel_restored),
                "removed": len(rel_removed),
                "changed": len(rel_changed),
                "unchanged": len(rel_unchanged),
            },
        },
    }


# ---------------------------------------------------------------- 响应整理

def build_history_view(version: int, source: str, created_at: str,
                       snap: dict) -> dict:
    """GET /api/assembly/versions/{v} 响应：历史版完整装配。"""
    return {
        "version": version,
        "source": source,
        "source_label": source_label(source),
        "created_at": created_at,
        "assembly": target_assembly(snap),
    }


def build_restore_preview(current_state: dict, version: int, source: str,
                          created_at: str, snap: dict) -> dict:
    """POST /api/assembly/versions/{v}/restore/preview 响应（不落库）。"""
    target = target_assembly(snap)
    return {
        "ok": True,
        "expected_version": current_state["version"],
        "target_version": version,
        "source": source,
        "source_label": source_label(source),
        "snapshot_created_at": created_at,
        "new_version": current_state["version"] + 1,
        "restored_assembly": target,
        "diff": diff_assembly(current_state, target),
    }
