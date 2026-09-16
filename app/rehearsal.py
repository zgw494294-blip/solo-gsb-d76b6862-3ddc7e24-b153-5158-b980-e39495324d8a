"""排演执行台 — 快照冻结与执行状态机。

规则
----
* 开始排演：把当前提示单（提示、依赖、计划时间）冻结为本场快照，
  之后对提示单的任何编辑 / 删除都不影响该场。
* 同一时刻只允许一场「进行中」的排演，重复开始返回 409。
* 提示执行状态：waiting(等待) → ready(就绪) → executing(执行中) → completed(已完成)。
  ready 是派生状态（不落库，读取时计算）：所有前置均已 completed，且
  会话经过时间 ≥ max(各前置实际完成时间 + 对应延迟, 锁定开场时间)；
  无前置且未锁定的提示立即就绪。
* 开始 / 完成由后端记录相对本场起点的秒数；非法状态跳转返回 409。
"""
from __future__ import annotations

import time

from fastapi import HTTPException

from .scheduler import build_schedule

EPS = 1e-6

# 排演状态
ACTIVE = "active"
ENDED = "ended"

# 提示执行状态（ready 为派生状态，不入库）
WAITING = "waiting"
READY = "ready"
EXECUTING = "executing"
COMPLETED = "completed"


def _now() -> float:
    return time.time()


def _elapsed(rehearsal: dict, now: float) -> float:
    """相对本场起点的秒数；已结束的场次冻结在结束时刻。"""
    end = rehearsal["end_epoch"] if rehearsal["end_epoch"] is not None else now
    return round(max(0.0, end - rehearsal["start_epoch"]), 3)


# ---------------------------------------------------------------- 开始 / 结束

def start_rehearsal(conn, name: str | None) -> dict:
    """冻结当前提示单为新一场排演；已有进行中排演时抛 409。"""
    active = conn.execute(
        "SELECT id FROM rehearsals WHERE status = ? LIMIT 1", (ACTIVE,)).fetchone()
    if active is not None:
        raise HTTPException(409, f"已有进行中的排演 #{active['id']}，请先结束本场")

    cues = [dict(r) for r in conn.execute(
        "SELECT id, department, name, duration, locked_start, sort_order "
        "FROM cues ORDER BY sort_order, id").fetchall()]
    if not cues:
        raise HTTPException(400, "提示单为空，无法开始排演")
    edges = [dict(r) for r in conn.execute(
        "SELECT cue_id, depends_on, delay FROM dependencies").fetchall()]
    planned = {c["id"]: c for c in build_schedule(cues, edges)["cues"]}

    now = _now()
    name = name or time.strftime("排演 %Y-%m-%d %H:%M:%S", time.localtime(now))
    cur = conn.execute(
        "INSERT INTO rehearsals (name, status, start_epoch) VALUES (?, ?, ?)",
        (name, ACTIVE, now))
    rid = cur.lastrowid
    for c in cues:
        p = planned.get(c["id"], {})
        conn.execute(
            "INSERT INTO rehearsal_cues (rehearsal_id, cue_id, department, name,"
            " duration, locked_start, sort_order, planned_start, planned_end)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, c["id"], c["department"], c["name"], c["duration"],
             c["locked_start"], c["sort_order"], p.get("start"), p.get("end")))
    for e in edges:
        conn.execute(
            "INSERT INTO rehearsal_deps (rehearsal_id, cue_id, depends_on, delay)"
            " VALUES (?, ?, ?, ?)",
            (rid, e["cue_id"], e["depends_on"], e["delay"]))
    conn.commit()
    return get_rehearsal(conn, rid)


def end_rehearsal(conn, rid: int) -> dict:
    """结束本场；已结束的场次再次结束返回 409。"""
    rehearsal = _require_active(conn, rid)
    conn.execute("UPDATE rehearsals SET status = ?, end_epoch = ? WHERE id = ?",
                 (ENDED, _now(), rehearsal["id"]))
    conn.commit()
    return get_rehearsal(conn, rid)


# ---------------------------------------------------------------- 状态机

def start_cue(conn, rid: int, cue_id: int) -> dict:
    """waiting 且满足就绪条件 → executing，记录 actual_start（相对秒）。"""
    rehearsal = _require_active(conn, rid)
    _, deps_by_cue, by_id = _load_snapshot(conn, rid)
    cue = by_id.get(cue_id)
    if cue is None:
        raise HTTPException(404, f"提示 #{cue_id} 不在本场排演快照中")
    if cue["status"] == EXECUTING:
        raise HTTPException(409, f"提示 #{cue_id} 已在执行中")
    if cue["status"] == COMPLETED:
        raise HTTPException(409, f"提示 #{cue_id} 已完成，不能重复开始")

    gate, preds_done = _compute_gate(cue, deps_by_cue.get(cue_id, []), by_id)
    if not preds_done:
        raise HTTPException(409, f"提示 #{cue_id} 的前置尚未全部完成，不能开始")
    elapsed = _elapsed(rehearsal, _now())
    if elapsed + EPS < gate:
        raise HTTPException(
            409, f"提示 #{cue_id} 尚未就绪：场钟需达到 {gate:.1f} 秒")
    conn.execute(
        "UPDATE rehearsal_cues SET status = ?, actual_start = ?"
        " WHERE rehearsal_id = ? AND cue_id = ?",
        (EXECUTING, round(elapsed, 3), rid, cue_id))
    conn.commit()
    return get_rehearsal(conn, rid)


def complete_cue(conn, rid: int, cue_id: int) -> dict:
    """executing → completed，记录 actual_end（相对秒）。"""
    rehearsal = _require_active(conn, rid)
    _, _, by_id = _load_snapshot(conn, rid)
    cue = by_id.get(cue_id)
    if cue is None:
        raise HTTPException(404, f"提示 #{cue_id} 不在本场排演快照中")
    if cue["status"] != EXECUTING:
        raise HTTPException(409, f"提示 #{cue_id} 当前不在执行中，不能完成")
    elapsed = _elapsed(rehearsal, _now())
    conn.execute(
        "UPDATE rehearsal_cues SET status = ?, actual_end = ?"
        " WHERE rehearsal_id = ? AND cue_id = ?",
        (COMPLETED, round(elapsed, 3), rid, cue_id))
    conn.commit()
    return get_rehearsal(conn, rid)


# ---------------------------------------------------------------- 读取

def get_current(conn) -> dict | None:
    """进行中的排演；若不存在则返回最近一场（可能已结束），无排演返回 None。"""
    row = conn.execute(
        "SELECT * FROM rehearsals "
        "ORDER BY (status = ?) DESC, id DESC LIMIT 1", (ACTIVE,)).fetchone()
    return _assemble(conn, dict(row)) if row else None


def get_rehearsal(conn, rid: int) -> dict:
    row = conn.execute("SELECT * FROM rehearsals WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise HTTPException(404, "排演不存在")
    return _assemble(conn, dict(row))


# ---------------------------------------------------------------- 内部

def _require_active(conn, rid: int) -> dict:
    row = conn.execute("SELECT * FROM rehearsals WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise HTTPException(404, "排演不存在")
    rehearsal = dict(row)
    if rehearsal["status"] != ACTIVE:
        raise HTTPException(409, f"排演 #{rid} 已结束，不能再执行操作")
    return rehearsal


def _load_snapshot(conn, rid: int):
    """读取本场快照：cues（按计划开始排序）、按提示分组的依赖、id 索引。"""
    cues = [dict(r) for r in conn.execute(
        "SELECT cue_id, department, name, duration, locked_start, sort_order,"
        " planned_start, planned_end, status, actual_start, actual_end"
        " FROM rehearsal_cues WHERE rehearsal_id = ?"
        " ORDER BY planned_start IS NULL, planned_start, sort_order, cue_id",
        (rid,)).fetchall()]
    deps = [dict(r) for r in conn.execute(
        "SELECT cue_id, depends_on, delay FROM rehearsal_deps"
        " WHERE rehearsal_id = ?", (rid,)).fetchall()]
    by_id = {c["cue_id"]: c for c in cues}
    deps_by_cue: dict[int, list[dict]] = {}
    for d in deps:
        deps_by_cue.setdefault(d["cue_id"], []).append(d)
    return cues, deps_by_cue, by_id


def _compute_gate(cue: dict, deps: list[dict], by_id: dict) -> tuple[float, bool]:
    """(就绪门限秒数, 前置是否全部完成)。

    门限 = max(各前置实际完成时间 + 对应延迟, 锁定开场时间)；
    无前置且未锁定时为 0（立即就绪）。
    """
    gate = 0.0
    preds_done = True
    for d in deps:
        pre = by_id.get(d["depends_on"])
        if pre is None or pre["status"] != COMPLETED:
            preds_done = False
            continue
        gate = max(gate, pre["actual_end"] + d["delay"])
    if cue["locked_start"] is not None:
        gate = max(gate, cue["locked_start"])
    return gate, preds_done


def _dev(actual, planned):
    if actual is None or planned is None:
        return None
    return round(actual - planned, 3)


def _assemble(conn, rehearsal: dict) -> dict:
    """组装排演完整视图：快照 + 实时状态（含派生 ready）+ 偏差。"""
    rid = rehearsal["id"]
    cues, deps_by_cue, by_id = _load_snapshot(conn, rid)
    now = _now()
    elapsed = _elapsed(rehearsal, now)
    out = []
    for c in cues:
        gate, preds_done = _compute_gate(c, deps_by_cue.get(c["cue_id"], []), by_id)
        status = c["status"]
        if status == WAITING and preds_done and elapsed + EPS >= gate:
            status = READY
        out.append({
            "cue_id": c["cue_id"],
            "department": c["department"],
            "name": c["name"],
            "duration": c["duration"],
            "locked_start": c["locked_start"],
            "planned_start": c["planned_start"],
            "planned_end": c["planned_end"],
            "status": status,
            "actual_start": c["actual_start"],
            "actual_end": c["actual_end"],
            "start_dev": _dev(c["actual_start"], c["planned_start"]),
            "end_dev": _dev(c["actual_end"], c["planned_end"]),
            "ready_at": round(gate, 3) if preds_done else None,
            "predecessors": [
                {"cue_id": d["depends_on"], "delay": d["delay"]}
                for d in sorted(deps_by_cue.get(c["cue_id"], []),
                                key=lambda x: x["depends_on"])],
        })
    return {
        "id": rehearsal["id"],
        "name": rehearsal["name"],
        "status": rehearsal["status"],
        "start_epoch": rehearsal["start_epoch"],
        "end_epoch": rehearsal["end_epoch"],
        "server_now": round(now, 3),
        "elapsed": elapsed,
        "total": len(cues),
        "completed": sum(1 for c in cues if c["status"] == COMPLETED),
        "cues": out,
    }
