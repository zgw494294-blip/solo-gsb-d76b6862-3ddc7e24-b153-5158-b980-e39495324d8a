/* 排演执行台 — 前端逻辑（原生 JS）
 *
 * 进行中的排演：1s 轮询同步状态（仅在数据变化时重渲染），100ms 刷新场钟；
 * 已结束 / 无排演：停止轮询，等待用户操作。
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    rehearsal: null,   // 当前场次（进行中或最近一场）
    clockOffset: 0,    // 服务器时间 − 客户端时间（秒），用于校准场钟
    lastFp: "",        // 上次渲染的数据指纹（不含场钟等易变字段）
    pollTimer: null,
    tickTimer: null,
    busy: false,
  };

  const STATUS_TEXT = {
    waiting: "等待", ready: "就绪", executing: "执行中", completed: "已完成",
  };

  // ----------------------------------------------------------- 工具

  function fmtTime(sec) {
    if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
    sec = Math.round(sec * 10) / 10;
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const ss = Number.isInteger(s) ? String(s).padStart(2, "0")
                                   : s.toFixed(1).padStart(4, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}`
                 : `${m}:${ss}`;
  }

  function fmtClock(sec) {
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const t = Math.floor((sec * 10) % 10);
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return `${String(h).padStart(2, "0")}:${mm}:${ss}.${t}`;
  }

  function fmtDev(dev) {
    if (dev === null || dev === undefined) return '<span class="muted">—</span>';
    const v = Math.round(dev * 10) / 10;
    const cls = v > 0 ? "dev-pos" : "dev-neg";
    return `<span class="${cls}">${v > 0 ? "+" : ""}${v}s</span>`;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try {
        const body = await res.json();
        msg = body.detail || msg;
      } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function setError(msg) {
    const el = $("#rehearsalError");
    el.classList.toggle("hidden", !msg);
    el.textContent = msg || "";
  }

  // ----------------------------------------------------------- 数据同步

  async function refresh(force = false) {
    try {
      const data = await api("/api/rehearsals/current");
      applyRehearsal(data.rehearsal, force);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }

  function applyRehearsal(r, force = false) {
    state.rehearsal = r;
    if (r) state.clockOffset = r.server_now - Date.now() / 1000;
    // 指纹只含影响表格内容的字段，场钟走动不触发整表重渲染
    const fp = JSON.stringify(r && [
      r.id, r.status, r.completed,
      r.cues.map((c) => [c.cue_id, c.status, c.actual_start, c.actual_end]),
    ]);
    if (force || fp !== state.lastFp) {
      state.lastFp = fp;
      render();
    }
    setupTimers();
  }

  function setupTimers() {
    const active = state.rehearsal && state.rehearsal.status === "active";
    if (active && !state.pollTimer) {
      state.pollTimer = setInterval(() => refresh(), 1000);
    } else if (!active && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (active && !state.tickTimer) {
      state.tickTimer = setInterval(tickClock, 100);
    } else if (!active && state.tickTimer) {
      clearInterval(state.tickTimer);
      state.tickTimer = null;
    }
  }

  function serverElapsed() {
    const r = state.rehearsal;
    if (!r) return 0;
    if (r.status !== "active") return r.elapsed; // 已结束：冻结在结束时刻
    return Math.max(0, Date.now() / 1000 + state.clockOffset - r.start_epoch);
  }

  function tickClock() {
    const el = $("#liveClock");
    if (el) el.textContent = fmtClock(serverElapsed());
  }

  // ----------------------------------------------------------- 渲染

  function startFormHtml(btnText) {
    return `
      <div class="rehearsal-start-row">
        <input type="text" id="rehearsalName" maxlength="120"
               placeholder="排演名称（可选，默认按时间命名）" />
        <button id="startRehearsalBtn" class="primary" type="button">▶ ${btnText}</button>
      </div>`;
  }

  function render() {
    const body = $("#rehearsalBody");
    const meta = $("#rehearsalMeta");
    const r = state.rehearsal;

    if (!r) {
      meta.textContent = "";
      body.innerHTML = `
        <div class="rehearsal-empty">
          <p>当前没有排演。开始排演会把<strong>当前提示单、依赖关系与计划时间</strong>冻结为本场快照，
          之后修改提示单不影响本场；同一时刻只允许一场进行中的排演。</p>
          ${startFormHtml("开始排演")}
        </div>`;
      return;
    }

    const active = r.status === "active";
    meta.innerHTML = active
      ? '<span class="badge badge-active">● 进行中</span>'
      : '<span class="badge badge-ended">已结束</span>';

    const rows = r.cues.map((c) => {
      let statusExtra = "";
      if (c.status === "waiting") {
        statusExtra = c.ready_at !== null && c.ready_at !== undefined
          ? `<div class="st-sub">就绪于 ${fmtTime(c.ready_at)}</div>`
          : '<div class="st-sub">等待前置完成</div>';
      }
      let ops = "";
      if (active) {
        if (c.status === "ready")
          ops = `<button class="primary sm" type="button" data-rstart="${c.cue_id}">开始</button>`;
        else if (c.status === "executing")
          ops = `<button class="primary sm" type="button" data-rcomplete="${c.cue_id}">完成</button>`;
        else if (c.status === "waiting")
          ops = '<button class="sm" type="button" disabled>开始</button>';
        else ops = '<span class="muted">✓</span>';
      }
      return `<tr class="st-row-${c.status}">
        <td class="num">${c.cue_id}</td>
        <td>${escapeHtml(c.department)}</td>
        <td>${escapeHtml(c.name)}${c.locked_start !== null && c.locked_start !== undefined
          ? ' <span class="lock-ico" title="锁定开场">🔒</span>' : ""}</td>
        <td><span class="st st-${c.status}">${STATUS_TEXT[c.status]}</span>${statusExtra}</td>
        <td class="num">${fmtTime(c.planned_start)}</td>
        <td class="num">${fmtTime(c.planned_end)}</td>
        <td class="num">${fmtTime(c.actual_start)}</td>
        <td class="num">${fmtTime(c.actual_end)}</td>
        <td class="num">${fmtDev(c.start_dev)} / ${fmtDev(c.end_dev)}</td>
        ${active ? `<td class="op-col">${ops}</td>` : ""}
      </tr>`;
    }).join("");

    body.innerHTML = `
      <div class="rehearsal-head">
        <div class="rehearsal-title">
          <strong>#${r.id} ${escapeHtml(r.name)}</strong>
          <span class="muted">已完成 ${r.completed}/${r.total}</span>
        </div>
        <div class="rehearsal-clock">
          <span class="clock-label">场钟</span>
          <span id="liveClock" class="clock">${fmtClock(r.elapsed)}</span>
        </div>
        ${active
          ? '<button id="endRehearsalBtn" class="danger-ghost" type="button">■ 结束本场</button>'
          : ""}
      </div>
      <div class="table-wrap">
        <table class="rehearsal-table">
          <thead><tr>
            <th>#</th><th>部门</th><th>名称</th><th>状态</th>
            <th>计划开始</th><th>计划结束</th>
            <th>实际开始</th><th>实际结束</th><th>偏差 始/终</th>
            ${active ? '<th class="op-col">操作</th>' : ""}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${active ? "" : `<div class="rehearsal-again">${startFormHtml("开始新排演")}</div>`}`;
  }

  // ----------------------------------------------------------- 操作

  async function startRehearsal() {
    if (state.busy) return;
    state.busy = true;
    try {
      const name = ($("#rehearsalName")?.value || "").trim();
      const r = await api("/api/rehearsals", {
        method: "POST",
        body: JSON.stringify(name ? { name } : {}),
      });
      applyRehearsal(r, true);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      state.busy = false;
    }
  }

  async function cueAction(cueId, action) {
    const r = state.rehearsal;
    if (!r || state.busy) return;
    state.busy = true;
    try {
      const updated = await api(
        `/api/rehearsals/${r.id}/cues/${cueId}/${action}`, { method: "POST" });
      applyRehearsal(updated, true);
      setError("");
    } catch (err) {
      setError(err.message);
      await refresh(true); // 可能已被其他端修改，重新同步
    } finally {
      state.busy = false;
    }
  }

  async function endRehearsal() {
    const r = state.rehearsal;
    if (!r || state.busy) return;
    if (!confirm(`确认结束本场排演「${r.name}」？\n结束后不能再执行提示操作。`)) return;
    state.busy = true;
    try {
      const updated = await api(`/api/rehearsals/${r.id}/end`, { method: "POST" });
      applyRehearsal(updated, true);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      state.busy = false;
    }
  }

  // ----------------------------------------------------------- 事件与启动

  $("#rehearsalBody").addEventListener("click", (e) => {
    if (e.target.closest("#startRehearsalBtn")) return startRehearsal();
    if (e.target.closest("#endRehearsalBtn")) return endRehearsal();
    const startBtn = e.target.closest("[data-rstart]");
    const completeBtn = e.target.closest("[data-rcomplete]");
    if (startBtn) return cueAction(startBtn.dataset.rstart, "start");
    if (completeBtn) return cueAction(completeBtn.dataset.rcomplete, "complete");
  });
  $("#rehearsalBody").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "rehearsalName") startRehearsal();
  });

  refresh(true);
})();
