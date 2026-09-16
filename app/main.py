"""舞台排演提示单 — FastAPI 入口。

启动时自动建表（SQLite 文件位于 CUE_DB_PATH），
所有增删改后即时按拓扑顺序级联重算，GET /api/schedule 返回完整时间轴数据。
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database, rehearsal
from .scheduler import CueNode, CyclicDependencyError, build_schedule, detect_cycle
from .schemas import CueCreate, CueUpdate, RehearsalStart

app = FastAPI(title="舞台排演提示单", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------- 数据读取

def _fetch_raw(conn) -> tuple[list[dict], list[dict]]:
    cues = [dict(r) for r in conn.execute(
        "SELECT id, department, name, duration, locked_start, sort_order "
        "FROM cues ORDER BY sort_order, id").fetchall()]
    edges = [dict(r) for r in conn.execute(
        "SELECT cue_id, depends_on, delay FROM dependencies").fetchall()]
    return cues, edges


def _reject_if_cyclic(conn) -> None:
    """对当前库中的完整依赖图做环检测；成环则抛 409。"""
    cues, edges = _fetch_raw(conn)
    node_map = {
        c["id"]: CueNode(id=c["id"], department="", name="",
                         duration=0, locked_start=None)
        for c in cues
    }
    preds: dict[int, list[int]] = {cid: [] for cid in node_map}
    for e in edges:
        if e["cue_id"] in preds and e["depends_on"] in preds:
            preds[e["cue_id"]].append(e["depends_on"])
    cyc = detect_cycle(node_map, preds)
    if cyc:
        raise CyclicDependencyError(cyc)


# ---------------------------------------------------------------- 启动

@app.on_event("startup")
def _startup() -> None:
    database.init_db()
    if os.environ.get("SEED_DEMO", "1") not in ("0", "false", "False", ""):
        database.seed_demo()


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/schedule")
def get_schedule() -> dict:
    """返回提示列表、依赖边、冲突链与成环信息（已完成级联重算）。"""
    with database.db() as conn:
        cues, edges = _fetch_raw(conn)
    return build_schedule(cues, edges)


@app.get("/api/cues/{cue_id}")
def get_cue(cue_id: int) -> dict:
    with database.db() as conn:
        row = conn.execute(
            "SELECT id, department, name, duration, locked_start, sort_order "
            "FROM cues WHERE id = ?", (cue_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "提示不存在")
        deps = [dict(r) for r in conn.execute(
            "SELECT depends_on AS id, delay FROM dependencies "
            "WHERE cue_id = ? ORDER BY depends_on", (cue_id,)).fetchall()]
    result = dict(row)
    result["predecessors"] = deps
    return result


def _write_dependencies(conn, cue_id: int, payload) -> None:
    """全量替换提示的前置依赖；非法输入直接抛 400。调用方负责回滚。"""
    conn.execute("DELETE FROM dependencies WHERE cue_id = ?", (cue_id,))
    seen: set[int] = set()
    for dep in payload.predecessors:
        if dep.id == cue_id:
            raise HTTPException(400, "提示不能依赖自身")
        if dep.id in seen:
            raise HTTPException(400, f"前置依赖 {dep.id} 重复")
        seen.add(dep.id)
        if conn.execute("SELECT 1 FROM cues WHERE id = ?",
                        (dep.id,)).fetchone() is None:
            raise HTTPException(400, f"前置提示 {dep.id} 不存在")
        conn.execute(
            "INSERT INTO dependencies (cue_id, depends_on, delay) "
            "VALUES (?, ?, ?)",
            (cue_id, dep.id, dep.delay),
        )


@app.post("/api/cues", status_code=201)
def create_cue(payload: CueCreate) -> dict:
    with database.db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO cues (department, name, duration, locked_start,"
                " sort_order) VALUES (?, ?, ?, ?, ?)",
                (payload.department, payload.name, payload.duration,
                 payload.locked_start, payload.sort_order),
            )
            cue_id = cur.lastrowid
            _write_dependencies(conn, cue_id, payload)
            # 新增依赖若成环必须拒绝
            _reject_if_cyclic(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"id": cue_id}


@app.put("/api/cues/{cue_id}")
def update_cue(cue_id: int, payload: CueUpdate) -> dict:
    with database.db() as conn:
        if conn.execute("SELECT id FROM cues WHERE id = ?",
                        (cue_id,)).fetchone() is None:
            raise HTTPException(404, "提示不存在")
        try:
            conn.execute(
                "UPDATE cues SET department=?, name=?, duration=?, "
                "locked_start=?, sort_order=? WHERE id=?",
                (payload.department, payload.name, payload.duration,
                 payload.locked_start, payload.sort_order, cue_id),
            )
            _write_dependencies(conn, cue_id, payload)
            _reject_if_cyclic(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"id": cue_id, "updated": True}


@app.delete("/api/cues/{cue_id}")
def delete_cue(cue_id: int) -> dict:
    with database.db() as conn:
        if conn.execute("SELECT id FROM cues WHERE id = ?",
                        (cue_id,)).fetchone() is None:
            raise HTTPException(404, "提示不存在")
        # 级联删除该提示、它的前置关系以及其它提示对它的依赖
        conn.execute(
            "DELETE FROM dependencies WHERE cue_id = ? OR depends_on = ?",
            (cue_id, cue_id))
        conn.execute("DELETE FROM cues WHERE id = ?", (cue_id,))
        conn.commit()
    return {"id": cue_id, "deleted": True}


@app.exception_handler(CyclicDependencyError)
def _cycle_handler(request, exc: CyclicDependencyError):
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "cycle": exc.cycle_ids},
    )


# ---------------------------------------------------------------- 排演执行台

@app.post("/api/rehearsals", status_code=201)
def start_rehearsal(payload: RehearsalStart | None = None) -> dict:
    """开始一场排演：冻结当前提示单 / 依赖 / 计划时间为快照。
    已有进行中的排演时返回 409。"""
    with database.db() as conn:
        return rehearsal.start_rehearsal(conn, payload.name if payload else None)


@app.get("/api/rehearsals/current")
def current_rehearsal() -> dict:
    """当前排演（进行中优先，否则最近一场）及实时执行状态。"""
    with database.db() as conn:
        return {"rehearsal": rehearsal.get_current(conn)}


@app.post("/api/rehearsals/{rid}/cues/{cue_id}/start")
def rehearsal_cue_start(rid: int, cue_id: int) -> dict:
    """开始执行某提示：记录相对本场起点的秒数；未就绪 / 状态非法返回 409。"""
    with database.db() as conn:
        return rehearsal.start_cue(conn, rid, cue_id)


@app.post("/api/rehearsals/{rid}/cues/{cue_id}/complete")
def rehearsal_cue_complete(rid: int, cue_id: int) -> dict:
    """完成某提示：记录相对本场起点的秒数；非执行中返回 409。"""
    with database.db() as conn:
        return rehearsal.complete_cue(conn, rid, cue_id)


@app.post("/api/rehearsals/{rid}/end")
def end_rehearsal(rid: int) -> dict:
    """结束本场排演；已结束时返回 409。"""
    with database.db() as conn:
        return rehearsal.end_rehearsal(conn, rid)


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
