/* 陶片拼接系统前端：Canvas 二维视图 + 录入 + 拼接 */
'use strict';

// ---------------------------------------------------------------- 状态
const S = {
  version: 0,
  fragments: [],
  groups: [],
  relations: [],
  mode: 'view',                       // view | draw | relate | revise
  cam: { scale: 6, ox: 0, oy: 0 },    // 像素/毫米 + 原点偏移
  draw: { name: '', points: [] },
  relate: { aId: null, bId: null, markersA: [], markersB: [], preview: null },
  // 轮廓修订会话：fid 修订片；points 编辑中的修订轮廓；relMarkers {rid: 局部标记}
  revise: null,
  // 锚点整体校准会话：anchorId 锚点；preview 预览结果（不落库）
  calibrate: null,
  trial: { steps: [], rehearsal: null, busy: false },
  // 历史版本：versions 版本清单；view 只读查看的历史版；restore 恢复预览
  history: { versions: [], view: null, restore: null, busy: false },
  // 离线工作站迁移：preview 导入预览（未确认，不写库）；busy 预览/确认中
  transfer: { preview: null, busy: false, fileName: '' },
  camInit: false,
};

const PALETTE = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
                 '#42a4f4', '#f032e6', '#9acd32', '#469990', '#9A6324'];
const groupColor = gid => gid == null ? '#a89f93' : PALETTE[gid % PALETTE.length];

const $ = sel => document.querySelector(sel);
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error('api error');
    err.status = res.status;
    err.detail = data.detail;
    throw err;
  }
  return data;
}

function detailText(detail) {
  if (!detail) return '请求失败';
  if (typeof detail === 'string') return detail;
  if (detail.conflicts && detail.conflicts.length)
    return detail.message + '：' + detail.conflicts.join('；');
  return detail.message || JSON.stringify(detail);
}

let msgTimer = null;
function setMessage(text, kind = 'ok') {
  const el = $('#message');
  el.textContent = text;
  el.className = kind;
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { el.textContent = ''; }, 8000);
}

async function loadAssembly() {
  const data = await api('/api/assembly');
  S.version = data.version;
  S.fragments = data.fragments;
  S.groups = data.groups;
  S.relations = data.relations;
  S.trial = { steps: [], rehearsal: null, busy: false };  // 刷新后旧试拼作废
  S.revise = null;                                        // 刷新后旧修订会话作废
  S.calibrate = null;                                     // 刷新后旧校准会话作废
  S.history.view = null;
  S.history.restore = null;
  S.transfer = { preview: null, busy: false, fileName: '' };
  $('#versionBadge').textContent = `版本 v${S.version}`;
  renderPanel();
  renderMain();
  loadVersions();
}

function handleConflict(err) {
  setMessage(detailText(err.detail), 'err');
  loadAssembly();   // 版本冲突：刷新到最新状态
}

// ---------------------------------------------------------------- 历史版本
const SOURCE_LABELS = {
  baseline: '基线', create: '录入碎片', delete: '删除碎片', accept: '采纳拼接',
  undo: '撤销拼接', trial: '有序试拼确认', revision: '轮廓修订确认',
  calibration: '锚点整体校准', restore: '版本恢复',
};
const sourceLabel = s => SOURCE_LABELS[s] || s;

async function loadVersions() {
  try {
    const data = await api('/api/assembly/versions');
    S.history.versions = data.versions;
    if (S.mode === 'view') renderPanel();
  } catch (err) { /* 清单加载失败不阻塞主视图 */ }
}

function closeHistory() {
  S.history.view = null;
  S.history.restore = null;
  renderPanel();
  renderMain();
}

async function viewVersion(v) {
  S.history.busy = true;
  renderPanel();
  try {
    const data = await api(`/api/assembly/versions/${v}`);
    S.history.view = data;
    S.history.restore = null;
    renderPanel();
    renderMain();
  } catch (err) {
    setMessage(detailText(err.detail), 'err');
  } finally {
    S.history.busy = false;
    renderPanel();
  }
}

async function previewRestore(v) {
  S.history.busy = true;
  renderPanel();
  try {
    const data = await api(`/api/assembly/versions/${v}/restore/preview`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: S.version }),
    });
    S.history.restore = data;
    S.history.view = null;
    renderPanel();
    renderMain();
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  } finally {
    S.history.busy = false;
    renderPanel();
  }
}

async function confirmRestore() {
  const pv = S.history.restore;
  if (!pv || pv.expected_version !== S.version) return;
  try {
    const res = await api(`/api/assembly/versions/${pv.target_version}/restore/confirm`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: S.version }),
    });
    setMessage(`已恢复到 v${res.restored_from_version} 的装配状态，装配版本 v${res.version}（旧快照仍可查）`);
    S.trial = { steps: [], rehearsal: null, busy: false };
    applyAssembly(res.assembly);
    loadVersions();
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

// 画布上优先展示的覆盖装配：导入预览（蓝）> 校准预览（蓝）> 恢复预览（蓝）> 只读历史版（紫）> 试拼预演（蓝）
function overlayAssembly() {
  const t = S.transfer.preview;
  if (t) return { kind: 'transfer', a: t.imported_assembly };
  const c = S.calibrate;
  if (c && c.preview) return { kind: 'calibration', a: calibrationAssembly() };
  const h = S.history;
  if (h.restore) return { kind: 'restore', a: h.restore.restored_assembly };
  if (h.view) return { kind: 'history', a: h.view.assembly };
  const r = S.trial.rehearsal;
  return r && r.ok ? { kind: 'trial', a: r.assembly } : null;
}

// ---------------------------------------------------------------- 有序试拼
function calibrationAssembly() {
  // 以当前装配为底，用预览的 after 位姿覆盖组内碎片（锚点 after==before）
  const pv = S.calibrate?.preview;
  if (!pv) return null;
  const after = Object.fromEntries(pv.poses.map(p => [p.fragment_id, p.after]));
  const fragments = S.fragments.map(f => {
    const a = after[f.id];
    return a ? { ...f, x: a.x, y: a.y, theta: a.theta_deg * Math.PI / 180 } : f;
  });
  return {
    version: S.version,
    calibration: true,
    fragments,
    groups: S.groups,
    relations: S.relations,
  };
}

function trialAssembly() {
  const r = S.trial.rehearsal;
  return r && r.ok ? r.assembly : null;   // 仅整体成功时在画布预演最终状态
}

function stepSummary(st) {
  if (st.action === 'undo') {
    if (st.client_id) return `撤销试拼关系「${esc(st.client_id)}」`;
    const rel = S.relations.find(r => r.id === st.relation_id);
    const name = rel
      ? `${esc(fragById(rel.fragment_a)?.name || '?')} ↔ ${esc(fragById(rel.fragment_b)?.name || '?')}`
      : `#${st.relation_id}`;
    return `撤销关系 #${st.relation_id}（${name}）`;
  }
  const an = fragById(st.fragment_a)?.name || '?';
  const bn = fragById(st.fragment_b)?.name || '?';
  const cid = st.client_id ? ` <span class="sub">[${esc(st.client_id)}]</span>` : '';
  return `建立关系 ${esc(an)} ↔ ${esc(bn)}${cid}`;
}

async function runRehearsal() {
  const T = S.trial;
  if (!T.steps.length) { T.rehearsal = null; return; }
  T.busy = true;
  try {
    T.rehearsal = await api('/api/trials/rehearse', {
      method: 'POST',
      body: JSON.stringify({ expected_version: S.version, steps: T.steps }),
    });
  } catch (err) {
    T.busy = false;
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
    T.rehearsal = null;
    renderPanel(); renderMain();
    return;
  }
  T.busy = false;
  renderPanel(); renderMain();
}

function addTrialStep(step) {
  S.trial.steps.push(step);
  S.trial.rehearsal = null;
  renderPanel(); renderMain();
  runRehearsal();
}

function removeTrialStep(i) {
  S.trial.steps.splice(i, 1);
  S.trial.rehearsal = null;
  if (!S.trial.steps.length) S.trial.rehearsal = null;
  renderPanel(); renderMain();
  if (S.trial.steps.length) runRehearsal();
}

function clearTrial() {
  S.trial = { steps: [], rehearsal: null, busy: false };
  renderPanel(); renderMain();
}

async function commitTrial() {
  const T = S.trial;
  try {
    const res = await api('/api/trials/commit', {
      method: 'POST',
      body: JSON.stringify({ expected_version: S.version, steps: T.steps }),
    });
    setMessage(`试拼序列已整体确认（${res.committed_steps} 步），装配版本 v${res.version}`);
    clearTrial();
    applyAssembly(res.assembly);
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

// ---------------------------------------------------------------- 坐标变换
function toScreen(x, y) { return [x * S.cam.scale + S.cam.ox, -y * S.cam.scale + S.cam.oy]; }
function toWorld(px, py) { return [(px - S.cam.ox) / S.cam.scale, -(py - S.cam.oy) / S.cam.scale]; }

function applyPose(p, f) {
  const c = Math.cos(f.theta), s = Math.sin(f.theta);
  return [c * p[0] - s * p[1] + f.x, s * p[0] + c * p[1] + f.y];
}
const worldContour = f => f.contour.map(p => applyPose(p, f));

function centroid(pts) {
  let x = 0, y = 0;
  for (const p of pts) { x += p[0]; y += p[1]; }
  return [x / pts.length, y / pts.length];
}

// ---------------------------------------------------------------- 主画布
function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!S.camInit && w > 0) {
    S.cam.ox = w / 2; S.cam.oy = h / 2; S.camInit = true;
  }
  return [w, h];
}

function drawGrid(w, h) {
  const steps = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500];
  let step = steps.find(s => s * S.cam.scale >= 30) || 1000;
  const labelEvery = Math.ceil(50 / (step * S.cam.scale));
  const [x0, yA] = toWorld(0, 0), [x1, yB] = toWorld(w, h);
  const yMin = Math.min(yA, yB), yMax = Math.max(yA, yB);
  ctx.lineWidth = 1;
  ctx.font = '10px sans-serif';
  ctx.fillStyle = '#9a8f7d';
  let i = Math.floor(x0 / step);
  for (let x = i * step; x <= x1; x += step, i++) {
    const [sx] = toScreen(x, 0);
    ctx.strokeStyle = i % 5 === 0 ? '#d8cfbc' : '#ece5d5';
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke();
    if (i % labelEvery === 0) ctx.fillText(`${+x.toFixed(1)}`, sx + 2, h - 4);
  }
  let j = Math.floor(yMin / step);
  for (let y = j * step; y <= yMax; y += step, j++) {
    const [, sy] = toScreen(0, y);
    ctx.strokeStyle = j % 5 === 0 ? '#d8cfbc' : '#ece5d5';
    ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(w, sy); ctx.stroke();
    if (j % labelEvery === 0) ctx.fillText(`${+y.toFixed(1)}`, 2, sy - 2);
  }
}

function pathOf(pts) {
  const p = new Path2D();
  pts.forEach((pt, i) => {
    const [sx, sy] = toScreen(pt[0], pt[1]);
    i === 0 ? p.moveTo(sx, sy) : p.lineTo(sx, sy);
  });
  p.closePath();
  return p;
}

function drawFragment(f, opts = {}) {
  const wc = opts.contour || worldContour(f);
  const color = opts.color || groupColor(f.group_id);
  const path = pathOf(wc);
  ctx.globalAlpha = opts.faint ? 0.25 : 1;
  ctx.fillStyle = color + '55';
  ctx.fill(path);
  ctx.strokeStyle = opts.highlight ? '#d35400' : color;
  ctx.lineWidth = opts.highlight ? 2.5 : 1.5;
  ctx.stroke(path);
  const [cx, cy] = centroid(wc);
  const [sx, sy] = toScreen(cx, cy);
  ctx.fillStyle = '#4a3f30';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(f.name || opts.name || '', sx, sy);
  ctx.textAlign = 'left';
  ctx.globalAlpha = 1;
}

function drawMarkerPair(wa, wb, idx, errText, color = '#c0392b') {
  const [ax, ay] = toScreen(wa[0], wa[1]);
  const [bx, by] = toScreen(wb[0], wb[1]);
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(ax, ay, 4, 0, 7); ctx.fill();
  ctx.strokeStyle = color;
  ctx.beginPath(); ctx.arc(bx, by, 4, 0, 7); ctx.stroke();
  ctx.fillStyle = color;
  ctx.font = '10px sans-serif';
  ctx.fillText(`#${idx + 1}${errText || ''}`, (ax + bx) / 2 + 5, (ay + by) / 2 - 4);
}

function renderMain() {
  const [w, h] = resizeCanvas();
  ctx.clearRect(0, 0, w, h);
  drawGrid(w, h);

  const pv = S.mode === 'relate' ? S.relate.preview : null;
  const pvIds = pv ? new Set(pv.placements.map(p => p.fragment_id)) : new Set();
  const revPv = S.mode === 'revise' ? S.revise.preview : null;

  // 有序试拼预演成功后：画布展示推演结果的最终位姿与分组；
  // 历史版本只读查看 / 恢复预览同样以覆盖装配的方式绘制（不改当前数据）
  const ov = S.mode === 'view' ? overlayAssembly() : null;
  const ovColor = ov && ov.kind === 'history' ? '#8e44ad' : '#2e86c1';  const viewFrags = ov ? ov.a.fragments : S.fragments;
  const viewRels = ov ? ov.a.relations : S.relations;

  for (const f of viewFrags) {
    if (pvIds.has(f.id)) continue;
    if (S.mode === 'revise' && f.id === S.revise.fid) continue;  // 修订片单独绘制
    const calAnchor = ov && ov.kind === 'calibration' && S.calibrate?.anchorId === f.id;
    drawFragment(f, { faint: S.mode === 'draw' || S.mode === 'revise',
      color: ov ? ovColor : groupColor(f.group_id),
      highlight: calAnchor });
  }

  // 轮廓修订：以现有位姿展示编辑中的修订轮廓（橙色高亮）
  if (S.mode === 'revise') {
    const f = fragById(S.revise.fid);
    const editing = { id: f.id, name: f.name, group_id: f.group_id,
                      contour: S.revise.points, x: f.x, y: f.y, theta: f.theta };
    if (S.revise.points.length >= 3) {
      drawFragment(editing, { color: '#d35400', highlight: true, name: f.name });
    }
    // 顶点编号叠加在修订轮廓上（局部 → 现有位姿 → 屏幕）
    S.revise.points.forEach((p, i) => {
      const [wx, wy] = applyPose(p, f);
      const [sx, sy] = toScreen(wx, wy);
      ctx.fillStyle = '#d35400';
      ctx.beginPath(); ctx.arc(sx, sy, 3.5, 0, 7); ctx.fill();
      ctx.fillStyle = '#6b5433'; ctx.font = '10px sans-serif';
      ctx.fillText(`${i + 1}`, sx + 5, sy - 5);
    });
    // 修订预览（ok 与否都画）：红色虚线连出受影响组各关系的对应点
    if (revPv) {
      const fragMap = Object.fromEntries(viewFrags.map(x => [x.id, x]));
      const revisedContour = revPv.contour || S.revise.points;
      fragMap[S.revise.fid] = { ...f, contour: revisedContour };
      for (const rw of revPv.relations) {
        const fa = fragMap[rw.fragment_a], fb = fragMap[rw.fragment_b];
        if (!fa || !fb || rw.markers_a.length !== rw.markers_b.length) continue;
        rw.markers_a.forEach((ma, i) => {
          drawMarkerPair(applyPose(ma, fa), applyPose(rw.markers_b[i], fb), i, '',
            rw.conflicts.length ? '#c0392b' : '#27ae60');
        });
      }
    }
  }

  // 预览：仅平移+旋转后的组合
  if (pv) {
    for (const pl of pv.placements) {
      drawFragment({ id: pl.fragment_id, name: pl.name, group_id: 1, contour: [] },
                   { contour: pl.contour_world, highlight: true, color: pv.ok ? '#2e86c1' : '#c0392b', name: pl.name });
    }
    if (pv.markers_world) {
      pv.markers_world.a.forEach((wa, i) => {
        const wb = pv.markers_world.b[i];
        const err = pv.errors[i] != null ? ` ${pv.errors[i].toFixed(2)}mm` : '';
        drawMarkerPair(wa, wb, i, err);
      });
    }
  }

  // 已有关系的标记（查看模式；历史版含已撤销关系，按覆盖装配绘制）
  if (S.mode === 'view') {
    const byId = Object.fromEntries(viewFrags.map(f => [f.id, f]));
    const calRels = ov && ov.kind === 'calibration' && S.calibrate?.preview
      ? Object.fromEntries(S.calibrate.preview.relations.map(r => [r.relation_id, r]))
      : {};
    for (const r of viewRels) {
      if (!r.active) continue;
      const fa = byId[r.fragment_a], fb = byId[r.fragment_b];
      if (!fa || !fb) continue;
      const isTrialRel = r.id < 0;   // 序列内新建关系的临时 id 为负数
      let color = ov && ov.kind === 'history' ? '#8e44ad'
        : isTrialRel ? '#2e86c1' : '#c0392b';
      if (calRels[r.id]) {
        const mx = calRels[r.id].max_error_after_mm ?? 0;
        color = mx <= 1 ? '#27ae60' : '#c0392b';
      }
      r.markers_a.forEach((m, i) => {
        drawMarkerPair(applyPose(m, fa), applyPose(r.markers_b[i], fb), i, '', color);
      });
    }
  }

  // 录入中的多边形
  if (S.mode === 'draw' && S.draw.points.length) {
    const pts = S.draw.points;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const [sx, sy] = toScreen(p[0], p[1]);
      i === 0 ? ctx.moveTo(sx, sy) : ctx.lineTo(sx, sy);
    });
    ctx.strokeStyle = '#c89b5a'; ctx.lineWidth = 2; ctx.stroke();
    pts.forEach((p, i) => {
      const [sx, sy] = toScreen(p[0], p[1]);
      ctx.fillStyle = '#c89b5a';
      ctx.beginPath(); ctx.arc(sx, sy, 3.5, 0, 7); ctx.fill();
      ctx.fillStyle = '#6b5433'; ctx.font = '10px sans-serif';
      ctx.fillText(`${i + 1}`, sx + 5, sy - 5);
    });
  }
}

// ---------------------------------------------------------------- 侧栏面板
const fragById = id => S.fragments.find(f => f.id === id);
const fmt = (v, n = 2) => (+v).toFixed(n);
const deg = r => (r * 180 / Math.PI).toFixed(2);

function renderPanel() {
  const el = $('#panel');
  if (S.mode === 'view') el.innerHTML = viewPanelHTML();
  else if (S.mode === 'draw') el.innerHTML = drawPanelHTML();
  else if (S.mode === 'revise') el.innerHTML = revisePanelHTML();
  else el.innerHTML = relatePanelHTML();
  bindPanelEvents();
  if (S.mode === 'relate') drawMinis();
  if (S.mode === 'revise') drawRevisionMinis();
  $('#hint').textContent = {
    view: S.transfer.preview
      ? `导入预览：蓝色为导入后的完整装配（碎片 ${S.transfer.preview.fragment_count} 片、`
        + `有效关系 ${S.transfer.preview.active_relation_count} 条）· 未写库 · 侧栏核对包内碎片/关系/快照后确认 · 拖拽平移 · 滚轮缩放`
      : S.calibrate && S.calibrate.preview
      ? `整体校准预览：锚点 #${S.calibrate.anchorId} 固定，蓝色为其余碎片调整后位姿（绿/红对应点通过/超限）· 未写库 · 侧栏核对前后位姿/逐关系误差/轮廓冲突后确认 · 拖拽平移 · 滚轮缩放`
      : S.calibrate
        ? `整体校准：已选锚点 #${S.calibrate.anchorId}（其位姿固定）· 点「请求预览」共同最小化组内误差 · 拖拽平移 · 滚轮缩放`
        : S.history.restore
      ? `恢复预览：恢复到 v${S.history.restore.target_version} 后的完整装配（蓝色，未改当前数据）· 侧栏可核对碎片/关系差异并确认恢复 · 拖拽平移 · 滚轮缩放`
      : S.history.view
        ? `历史版本 v${S.history.view.version} 只读查看（紫色，未改当前数据）· 碎片轮廓/位姿/连通组/有效及已撤销关系 · 拖拽平移 · 滚轮缩放`
        : trialAssembly()
          ? '有序试拼预演结果（蓝色，不落库）· 侧栏可确认整组提交 · 拖拽平移 · 滚轮缩放'
          : '拖拽平移 · 滚轮缩放 · 单位：毫米',
    draw: '单击画布添加顶点（≥3 个）· 拖拽平移 · 滚轮缩放 · 保存时自动闭合',
    relate: '在左侧两个小图中点击轮廓边缘放置对应标记（每片 ≥2 处、数量一致），然后预览/采纳/加入试拼',
    revise: '在主画布单击添加修订轮廓顶点（≥3 个，保存时自动闭合）；在各关系小图中点击「本片」一侧的修订轮廓边缘放置新标记；先预览再确认',
  }[S.mode];
}

function viewPanelHTML() {
  // 历史版本只读查看 / 恢复预览：顶部横幅 + 覆盖装配卡片，当前数据不隐藏但不渲染操作
  if (S.history.restore) return restorePanelHTML();
  if (S.history.view) return historyViewPanelHTML();

  const active = S.relations.filter(r => r.active);
  const undone = S.relations.filter(r => !r.active);
  const fragCards = S.fragments.map(f => `
    <div class="card">
      <div class="row">
        <span><span class="dot" style="background:${groupColor(f.group_id)}"></span><b>#${f.id} ${esc(f.name)}</b></span>
        <span class="spacer"></span>
        <button class="act" data-calib-frag="${f.id}">整体校准</button>
        <button class="act" data-revise-frag="${f.id}">修订轮廓</button>
        <button class="act danger" data-del-frag="${f.id}">删除</button>
      </div>
      <div class="sub">${f.group_id == null ? '独立碎片' : `拼接组 G${f.group_id}`} ·
        位置 (${fmt(f.x)}, ${fmt(f.y)}) mm · 角度 ${deg(f.theta)}° · ${f.contour.length} 顶点</div>
    </div>`).join('') || '<div class="small">暂无碎片，请切换到「录入碎片」。</div>';
  const relCards = active.map(r => `
    <div class="card">
      <div class="row">
        <span><b>#${r.id}</b> ${esc(fragById(r.fragment_a)?.name || '?')} ↔ ${esc(fragById(r.fragment_b)?.name || '?')}</span>
        <span class="spacer"></span>
        <button class="act" data-trial-undo-rel="${r.id}">试拼撤销</button>
        <button class="act" data-undo="${r.id}">撤销</button>
      </div>
      <div class="sub">${r.markers_a.length} 对标记 · 最大误差 ${fmt(r.max_error, 3)} mm · ${r.created_at.slice(0, 19)}</div>
    </div>`).join('') || '<div class="small">暂无有效拼接关系。</div>';
  const undoneCards = undone.map(r => `
    <div class="card undone"><b>#${r.id}</b> ${esc(fragById(r.fragment_a)?.name || r.fragment_a)} ↔
      ${esc(fragById(r.fragment_b)?.name || r.fragment_b)} <span class="sub">已撤销</span></div>`).join('');
  return `
    ${calibrationBannerHTML()}
    <h2>碎片（${S.fragments.length}）</h2>${fragCards}
    <h2>拼接关系（${active.length}）</h2>${relCards}
    ${undone ? `<h2>已撤销（${undone.length}）</h2>${undoneCards}` : ''}
    ${historyPanelHTML()}
    ${transferPanelHTML()}
    ${trialPanelHTML()}`;
}

// ---------------------------------------------------------------- 锚点整体校准面板
function calibrationBannerHTML() {
  const C = S.calibrate;
  if (!C) return '';
  const anchor = fragById(C.anchorId);
  const pv = C.preview;
  if (!pv) {
    return `
    <div class="okbox hist-banner">🎯 整体校准：锚点 <b>#${C.anchorId} ${esc(anchor?.name || '')}</b>
      （位姿/轮廓/标记/关系/分组均不变）· 基于 v${S.version}，预览不落库。</div>
    <div class="row" style="margin:6px 0">
      <button class="act primary" id="btnCalibPreview">请求预览</button>
      <button class="act danger" id="btnCalibClose">取消</button>
    </div>`;
  }
  const stale = pv.expected_version !== S.version;
  const poseRows = pv.poses.map(p => `
    <tr><td>${p.anchor ? '🎯 ' : ''}#${p.fragment_id} ${esc(p.name)}</td>
      <td>${fmt(p.before.x)}</td><td>${fmt(p.before.y)}</td><td>${fmt(p.before.theta_deg)}°</td>
      <td>${fmt(p.after.x)}</td><td>${fmt(p.after.y)}</td><td>${fmt(p.after.theta_deg)}°</td>
      <td>${p.anchor ? '固定' : (p.before.x === p.after.x && p.before.y === p.after.y
        && p.before.theta_deg === p.after.theta_deg ? '未移动' : '调整')}</td></tr>`).join('');
  const errRows = pv.relations.map(r => {
    const cells = r.errors_after.map((e, i) => {
      const b = r.errors_before[i];
      const lower = e < b - 1e-6, higher = e > b + 1e-6;
      const cls = e <= 1 ? 'oktxt' : 'errtxt';
      const arrow = lower ? '↓' : higher ? '↑' : '＝';
      return `<span class="${cls}">${fmt(b, 3)}→${fmt(e, 3)} ${arrow}</span>`;
    }).join('；');
    return `<tr><td>#${r.relation_id}「${esc(r.name_a)}」↔「${esc(r.name_b)}」</td>
      <td>${cells || '—'}</td>
      <td class="${(r.max_error_after_mm ?? 0) <= 1 ? 'oktxt' : 'errtxt'}">
        ${r.max_error_after_mm == null ? '-' : fmt(r.max_error_after_mm, 3) + ' mm'}</td></tr>`;
  }).join('');
  const ovRows = pv.overlaps.map(o =>
    `<li>碎片「${esc(o.name_a)}」与「${esc(o.name_b)}」轮廓内部重叠 ${fmt(o.area_mm2, 2)} mm²</li>`).join('');
  return `
    <div class="okbox hist-banner">🎯 整体校准预览：锚点 <b>#${pv.anchor_id} ${esc(anchor?.name || '')}</b>
      固定，仅平移+旋转组内其余碎片 · 基于 v${pv.expected_version}，<b>未写库</b>。
      主画布蓝色为调整后位姿（绿/红 = 对应点通过/超限）。</div>
    ${stale ? `<div class="conflicts">预览依据的版本 v${pv.expected_version} 已过期（当前 v${S.version}），
       请重新预览；此预览不可确认。</div>` : ''}
    <div class="row" style="margin:6px 0">
      <button class="act" id="btnCalibPreview">重新预览</button>
      <button class="act primary" id="btnCalibConfirm"
        ${pv.ok && !stale ? '' : 'disabled'}>确认校准（v${S.version} → v${S.version + 1}）</button>
      <button class="act danger" id="btnCalibClose">取消</button>
    </div>
    ${pv.ok
      ? `<div class="okbox">✓ 满足确认条件：误差总量严格下降（${fmt(pv.sse_before_mm2, 4)} →
          ${fmt(pv.sse_after_mm2, 4)} mm²）、每对对应点误差均 ≤ 1 mm、组内轮廓内部不重叠。
          轮廓/标记/关系状态/分组不变。</div>`
      : `<div class="conflicts"><b>不可确认（保持现状），原因：</b>
          <ul>${pv.conflicts.map(c => `<li>${esc(c)}</li>`).join('')}</ul></div>`}
    <table class="info">
      <tr><th>指标</th><th>校准前</th><th>校准后</th></tr>
      <tr><td>误差总量（mm²）</td><td>${fmt(pv.sse_before_mm2, 4)}</td>
        <td class="${pv.sse_strictly_decreased ? 'oktxt' : 'errtxt'}">${fmt(pv.sse_after_mm2, 4)}
        （${pv.sse_strictly_decreased ? '严格下降' : '未下降'}）</td></tr>
      <tr><td>最大对应点误差（mm）</td><td>${fmt(pv.max_error_before_mm, 3)}</td>
        <td class="${pv.all_pair_errors_within_1mm ? 'oktxt' : 'errtxt'}">${fmt(pv.max_error_after_mm, 3)}
        （${pv.all_pair_errors_within_1mm ? '≤1 mm' : '超限'}）</td></tr>
      <tr><td>轮廓内部重叠（对）</td><td>—</td>
        <td class="${pv.overlaps.length ? 'errtxt' : 'oktxt'}">${pv.overlaps.length}</td></tr>
    </table>
    <table class="info">
      <tr><th>碎片</th><th>x 前</th><th>y 前</th><th>角度前</th>
          <th>x 后</th><th>y 后</th><th>角度后</th><th></th></tr>
      ${poseRows}
    </table>
    <table class="info">
      <tr><th>关系（逐对对应点误差，mm）</th><th>校准前 → 校准后</th><th>校准后最大</th></tr>
      ${errRows}
    </table>
    ${ovRows ? `<div class="conflicts"><b>轮廓冲突：</b><ul>${ovRows}</ul></div>` : ''}`;
}

// ---------------------------------------------------------------- 历史版本面板
function groupText(groups) {
  return groups.length
    ? groups.map(g => `G${g.id}: ${g.fragment_ids.join(', ')}`).join('；')
    : '（全部独立）';
}

function historyPanelHTML() {
  const H = S.history;
  const rows = H.versions.map(v => `
    <div class="card ${v.current ? 'hist-cur' : ''}">
      <div class="row">
        <span><b>v${v.version}</b> <span class="sub">${esc(sourceLabel(v.source))}</span></span>
        <span class="spacer"></span>
        ${v.current ? '<span class="sub oktxt">当前</span>'
          : `<button class="act" data-hist-view="${v.version}" ${H.busy ? 'disabled' : ''}>查看</button>
             <button class="act primary" data-hist-restore="${v.version}" ${H.busy ? 'disabled' : ''}>恢复预览</button>`}
      </div>
      <div class="sub">${v.created_at.replace('T', ' ').slice(0, 19)}</div>
    </div>`).join('');
  return `
    <h2>历史版本（${H.versions.length}）</h2>
    <div class="small">每次成功变更都与装配数据原子保存不可改快照；查看只读，
      「恢复预览」先展示恢复结果及与当前版差异，确认后版本只 +1，旧快照仍可查。</div>
    ${rows || `<div class="small">${H.busy ? '加载中…' : '暂无快照'}</div>`}`;
}

// ---------------------------------------------------------------- 离线工作站迁移
function transferPanelHTML() {
  const T = S.transfer;
  const pv = T.preview;
  let pvHTML = '';
  if (pv) {
    const p = pv.package;
    const stale = pv.target.current_version !== S.version;
    const versionRows = p.versions.map(s => `
      <tr><td>v${s.version}</td><td>${esc(sourceLabel(s.source))}</td>
      <td>${(s.created_at || '').replace('T', ' ').slice(0, 19)}</td></tr>`).join('');
    pvHTML = `
      <div class="${pv.ok ? 'okbox' : 'conflicts'} hist-banner">
        ${pv.ok
          ? `📦 导入预览（<b>未写库</b>）：包 ${esc(p.format)} <span class="mono">v${esc(p.format_version)}</span>
             导出于 ${esc((p.exported_at || '').replace('T', ' ').slice(0, 19))}，
             含 <b>${p.snapshot_count}</b> 个不可改快照（v0/v1 → v${p.current_version}）、
             <b>${pv.fragment_count}</b> 片碎片、<b>${pv.active_relation_count}</b> 条有效关系、
             <b>${pv.undone_relation_count}</b> 条已撤销关系；导入后装配版本 <b>v${pv.imported_version}</b>
             （原 ID、轮廓、位姿、标记、状态与时间戳全部保留，基线由包替换）。`
          : `📦 导入预览：<b>目标库不是空白工作站，导入将被整包拒绝</b>。
             ${pv.reasons.map(r => `<div>${esc(r)}</div>`).join('')}
             <div class="small">现状：${pv.target.fragment_count} 片碎片、
               ${pv.target.relation_count} 条关系、${pv.target.snapshot_count} 个快照
               （当前版本 v${pv.target.current_version}）。</div>`}
      </div>
      ${stale ? `<div class="conflicts">目标库当前版本已变为 v${S.version}
         （预览基于 v${pv.target.current_version}），此预览不可确认，请重新预览。</div>` : ''}
      <h2>导入后连通组</h2><div class="small">${esc(groupText(pv.imported_assembly.groups))}</div>
      <h2>包内碎片（${pv.fragments.length}）</h2>${historyFragCards(pv.imported_assembly)}
      <h2>包内关系（${pv.active_relation_count} 有效 / ${pv.undone_relation_count} 已撤销）</h2>
      ${historyRelCards(pv.imported_assembly)}
      <h2>包内快照（${p.snapshot_count}）</h2>
      <table class="info"><tr><th>版本</th><th>来源</th><th>快照时间（原样保留）</th></tr>${versionRows}</table>
      <div class="row" style="margin:8px 0">
        <button class="act primary" id="btnTransferConfirm"
          ${pv.ok && !stale && !T.busy ? '' : 'disabled'}>确认导入（整包替换空白基线）</button>
        <button class="act danger" id="btnTransferClose" ${T.busy ? 'disabled' : ''}>取消</button>
      </div>`;
  }
  return `
    <h2>离线工作站迁移</h2>
    <div class="small">更换离线工作站时，把当前装配及全部不可改版本快照导出为
      带格式版本号的 JSON 包，再在另一台<b>空白</b>工作站导入：先预览包内碎片、
      有效及已撤销关系、版本数与导入后装配（不写库），确认须携带目标库当前
      装配版本，单事务保留全部原 ID / 轮廓 / 位姿 / 标记 / 状态 / 时间戳及
      各快照原版本号；导入后历史查看、恢复与拼接照常使用。</div>
    <div class="row" style="margin:6px 0">
      <button class="act" id="btnTransferExport" ${T.busy ? 'disabled' : ''}>导出当前装配包（JSON）</button>
    </div>
    <label class="lab">在空白工作站选择迁移包（.json）</label>
    <input type="file" id="transferFile" accept="application/json,.json">
    <div class="row" style="margin:6px 0">
      <button class="act" id="btnTransferPreview" ${T.busy ? 'disabled' : ''}>导入预览（不写库）</button>
    </div>
    ${pvHTML}`;
}

async function exportPackage() {
  try {
    const res = await fetch('/api/transfer/export?download=true');
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw Object.assign(new Error('api error'), { status: res.status, detail: data.detail });
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `pottery-assembly-v${S.version}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setMessage('迁移包已导出（含当前装配与全部不可改快照）');
  } catch (err) {
    setMessage(detailText(err.detail), 'err');
  }
}

function transferReasonsText(detail) {
  if (!detail) return '请求失败';
  if (Array.isArray(detail.reasons) && detail.reasons.length)
    return (detail.message || '迁移包整次拒绝') + '：' + detail.reasons.join('；');
  return detail.message || JSON.stringify(detail);
}

async function previewImport() {
  const file = $('#transferFile').files[0];
  if (!file) { setMessage('请先选择迁移包 JSON 文件', 'err'); return; }
  let pkg;
  try {
    pkg = JSON.parse(await file.text());
  } catch {
    setMessage('迁移包不是合法的 JSON 文件', 'err');
    return;
  }
  const T = S.transfer;
  T.busy = true;
  renderPanel();
  try {
    T.preview = await api('/api/transfer/import/preview', {
      method: 'POST',
      body: JSON.stringify({ package: pkg }),
    });
    T.fileName = file.name;
    renderPanel(); renderMain();
    setMessage(T.preview.ok
      ? '导入预览完成（未写库）：核对后可确认导入'
      : '目标库不是空白工作站，无法导入');
  } catch (err) {
    T.preview = null;
    renderPanel(); renderMain();
    setMessage(transferReasonsText(err.detail), 'err');
  } finally {
    T.busy = false;
    renderPanel();
  }
}

async function confirmImport() {
  const pv = S.transfer.preview;
  if (!pv || !pv.ok) return;
  const file = $('#transferFile').files[0];
  if (!file) { setMessage('请重新选择迁移包 JSON 文件', 'err'); return; }
  let pkg;
  try {
    pkg = JSON.parse(await file.text());
  } catch {
    setMessage('迁移包不是合法的 JSON 文件', 'err');
    return;
  }
  S.transfer.busy = true;
  renderPanel();
  try {
    const res = await api('/api/transfer/import/confirm', {
      method: 'POST',
      body: JSON.stringify({ package: pkg, expected_version: S.version }),
    });
    setMessage(`导入完成：装配版本 v${res.imported_version}、${res.snapshot_count} 个快照已原样保留，历史查看 / 恢复 / 拼接可继续使用`);
    const fileEl = $('#transferFile');
    if (fileEl) fileEl.value = '';
    S.transfer = { preview: null, busy: false, fileName: '' };
    S.calibrate = null;
    S.trial = { steps: [], rehearsal: null, busy: false };
    applyAssembly(res.assembly);
  } catch (err) {
    S.transfer.busy = false;
    if (err.status === 409) { renderPanel(); return handleConflict(err); }
    renderPanel();
    setMessage(transferReasonsText(err.detail), 'err');
  }
}

function closeTransferPreview() {
  S.transfer.preview = null;
  renderPanel(); renderMain();
}

function historyFragCards(asm) {
  return asm.fragments.map(f => `
    <div class="card">
      <div class="row">
        <span><span class="dot" style="background:${groupColor(f.group_id)}"></span><b>#${f.id} ${esc(f.name)}</b></span>
      </div>
      <div class="sub">${f.group_id == null ? '独立碎片' : `拼接组 G${f.group_id}`} ·
        位置 (${fmt(f.x)}, ${fmt(f.y)}) mm · 角度 ${deg(f.theta)}° · ${f.contour.length} 顶点</div>
    </div>`).join('');
}

function historyRelCards(asm) {
  return asm.relations.map(r => `
    <div class="card ${r.active ? '' : 'undone'}">
      <b>#${r.id}</b> ${esc(asm.fragments.find(f => f.id === r.fragment_a)?.name || r.fragment_a)} ↔
      ${esc(asm.fragments.find(f => f.id === r.fragment_b)?.name || r.fragment_b)}
      <div class="sub">${r.active
        ? `有效 · ${r.markers_a.length} 对标记 · 最大误差 ${fmt(r.max_error, 3)} mm`
        : `已撤销 · 撤销于 ${(r.undone_at || '').replace('T', ' ').slice(0, 19)}`}</div>
    </div>`).join('') || '<div class="small">该版本无任何关系。</div>';
}

function historyViewPanelHTML() {
  const hv = S.history.view;
  const a = hv.assembly;
  return `
    <div class="okbox hist-banner">📜 只读查看历史版 <b>v${hv.version}</b>
      （${esc(hv.source_label)}）— 当前数据未改变。</div>
    <div class="row" style="margin:6px 0">
      <button class="act" id="btnHistBack">返回当前装配</button>
      ${hv.version === S.version ? ''
        : `<button class="act primary" id="btnHistRestore" ${S.history.busy ? 'disabled' : ''}>恢复到此版本…</button>`}
    </div>
    <h2>连通组</h2><div class="small">${esc(groupText(a.groups))}</div>
    <h2>碎片（${a.fragments.length}）</h2>${historyFragCards(a)}
    <h2>关系（${a.relations.length}，含已撤销）</h2>${historyRelCards(a)}`;
}

function diffEntryHtml(entry, kind, type) {
  const labelMap = {
    fragment: { restored: '将重现', removed: '将删除', changed: '将变更' },
    relation: { restored: '将恢复', removed: '将删除', changed: '将变更' },
  };
  return entry.map(x => {
    if (kind === 'fragment') {
      const obj = x.target || x.current;
      return `<div class="small">碎片 #${x.id}「${esc(obj.name)}」${labelMap.fragment[type]}`
        + (type === 'changed' ? `（${esc(x.changed_fields.join('、'))}）` : '') + '</div>';
    }
    const obj = x.target || x.current;
    const stateTxt = type === 'restored' ? '有效'
      : type === 'removed' ? (obj.active ? '有效' : '已撤销')
      : (x.target.active ? '由撤销→有效' : '由有效→撤销');
    return `<div class="small">关系 #${x.id}（#${obj.fragment_a} ↔ #${obj.fragment_b}，${stateTxt}）`
      + (type === 'changed' ? `：${esc(x.changed_fields.join('、'))}` : '') + '</div>';
  }).join('');
}

function restorePanelHTML() {
  const pv = S.history.restore;
  const a = pv.restored_assembly;
  const d = pv.diff;
  const stale = pv.expected_version !== S.version;
  const fs = d.summary.fragments, rs = d.summary.relations;
  const noChange = !fs.restored && !fs.removed && !fs.changed
    && !rs.restored && !rs.removed && !rs.changed;
  return `
    <div class="okbox hist-banner">↩️ 恢复预览：<b>v${pv.target_version}</b>（${esc(pv.source_label)}）
      → 确认后新版本 <b>v${pv.new_version}</b>。当前 v${S.version} 数据尚未改变。</div>
    ${stale ? `<div class="conflicts">预览依据的版本 v${pv.expected_version} 已过期（当前 v${S.version}），
       请返回后重新预览；此预览不可确认。</div>` : ''}
    <div class="row" style="margin:6px 0">
      <button class="act" id="btnHistBack">返回当前装配</button>
      <button class="act primary" id="btnHistConfirm"
        ${stale || S.history.busy ? 'disabled' : ''}>确认恢复（版本 v${S.version} → v${pv.new_version}）</button>
    </div>
    <h2>与当前版差异</h2>
    ${noChange ? '<div class="small">两版装配内容一致（仅版本号不同），恢复不会改变碎片与关系。</div>' : ''}
    ${fs.restored ? `<div class="sub oktxt">碎片重现 ${fs.restored}</div>${diffEntryHtml(d.fragments.restored, 'fragment', 'restored')}` : ''}
    ${fs.removed ? `<div class="sub errtxt">碎片删除 ${fs.removed}</div>${diffEntryHtml(d.fragments.removed, 'fragment', 'removed')}` : ''}
    ${fs.changed ? `<div class="sub errtxt">碎片变更 ${fs.changed}</div>${diffEntryHtml(d.fragments.changed, 'fragment', 'changed')}` : ''}
    ${rs.restored ? `<div class="sub oktxt">关系恢复 ${rs.restored}</div>${diffEntryHtml(d.relations.restored, 'relation', 'restored')}` : ''}
    ${rs.removed ? `<div class="sub errtxt">关系删除 ${rs.removed}</div>${diffEntryHtml(d.relations.removed, 'relation', 'removed')}` : ''}
    ${rs.changed ? `<div class="sub errtxt">关系变更 ${rs.changed}</div>${diffEntryHtml(d.relations.changed, 'relation', 'changed')}` : ''}
    <div class="small">碎片不变 ${fs.unchanged} · 关系不变 ${rs.unchanged}</div>
    <h2>恢复后连通组</h2><div class="small">${esc(groupText(a.groups))}</div>
    <h2>恢复后碎片（${a.fragments.length}）</h2>${historyFragCards(a)}
    <h2>恢复后关系（${a.relations.length}，含已撤销）</h2>${historyRelCards(a)}`;
}

function trialPanelHTML() {
  const T = S.trial;
  const stepCards = T.steps.map((st, i) => `
    <div class="card ${i === (T.rehearsal?.failed_at || 0) - 1 ? 'trial-fail' : ''}">
      <div class="row">
        <span class="mono">${i + 1}.</span>
        <span class="spacer"></span>
        <button class="act danger" data-trial-del="${i}">移除</button>
      </div>
      <div>${stepSummary(st)}</div>
      ${T.rehearsal && T.rehearsal.steps[i]
        ? (T.rehearsal.steps[i].ok ? '<div class="sub oktxt">✓ 可执行</div>'
            : `<div class="sub errtxt">✗ ${T.rehearsal.steps[i].conflicts.map(esc).join('；')}</div>`)
        : ''}
    </div>`).join('');

  let resultHTML = '';
  if (T.steps.length) {
    const r = T.rehearsal;
    if (!r) {
      resultHTML = `<div class="small">${T.busy ? '预演中…' : ''}</div>`;
    } else if (r.ok) {
      const rows = r.assembly.fragments.map(f =>
        `<tr><td>${esc(f.name)}</td><td>${fmt(f.x)}</td><td>${fmt(f.y)}</td><td>${deg(f.theta)}°</td><td>${f.group_id == null ? '独立' : 'G' + f.group_id}</td></tr>`).join('');
      const groups = r.assembly.groups.map(g =>
        `G${g.id}: ${g.fragment_ids.join(', ')}`).join('；') || '（全部独立）';
      resultHTML = `
        <div class="okbox">✓ 预演通过：${r.steps.length} 步全部可执行，确认后版本仅递增一次（v${r.base_version} → v${r.base_version + 1}）。</div>
        <div class="small"><b>最终连通组</b>：${esc(groups)}</div>
        <table class="info"><tr><th>碎片</th><th>x</th><th>y</th><th>角度</th><th>分组</th></tr>${rows}</table>
        <button class="act primary" id="btnTrialCommit">确认整组提交</button>`;
    } else {
      const f = r.failure;
      resultHTML = `
        <div class="conflicts"><b>第 ${f.step} 步失败（序列在此中止，确认不会写入任何变化）：</b>
          <div>${esc(f.message)}</div>
          ${f.conflicts && f.conflicts.length ? `<ul>${f.conflicts.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
        </div>`;
    }
  }

  return `
    <h2>有序试拼（${T.steps.length} 步）</h2>
    ${stepCards || '<div class="small">在「新建拼接」中编排“建立关系”，或点关系旁的「试拼撤销」加入撤销步。预演不落库。</div>'}
    ${T.steps.length ? '<div class="row" style="margin:6px 0"><button class="act danger" id="btnTrialClear">清空序列</button></div>' : ''}
    ${resultHTML}`;
}

function drawPanelHTML() {
  const pts = S.draw.points.map((p, i) =>
    `<div class="small mono">${i + 1}. (${fmt(p[0])}, ${fmt(p[1])})</div>`).join('');
  return `
    <h2>录入碎片</h2>
    <label class="lab">名称</label>
    <input type="text" id="fragName" value="${esc(S.draw.name)}" placeholder="如：陶片E-口沿">
    <label class="lab">顶点（毫米，${S.draw.points.length} 个）</label>
    <div style="max-height:180px;overflow:auto">${pts || '<div class="small">在主画布上单击添加顶点</div>'}</div>
    <div class="row" style="margin-top:8px">
      <button class="act" id="undoPt" ${S.draw.points.length ? '' : 'disabled'}>撤销顶点</button>
      <button class="act" id="clearPts" ${S.draw.points.length ? '' : 'disabled'}>清空</button>
    </div>
    <div class="row" style="margin-top:6px">
      <button class="act primary" id="saveFrag" ${S.draw.points.length >= 3 ? '' : 'disabled'}>保存碎片</button>
      <button class="act" id="cancelDraw">取消</button>
    </div>
    <h2>或粘贴坐标 JSON</h2>
    <textarea id="jsonPts" rows="4" placeholder='[[0,0],[50,0],[50,40],[0,40]]'></textarea>
    <div class="row" style="margin-top:6px"><button class="act" id="loadJson">载入坐标</button></div>`;
}

function relatePanelHTML() {
  const R = S.relate;
  const opts = sel => S.fragments.map(f =>
    `<option value="${f.id}" ${f.id === sel ? 'selected' : ''}>#${f.id} ${esc(f.name)}</option>`).join('');
  const pv = R.preview;
  let pvHTML = '';
  if (pv) {
    const typeName = { new_group: '新建拼接组', join: '加入已有组', merge: '合并两个组', loop: '闭环校验' }[pv.relation_type] || '-';
    const rows = pv.placements.map(p =>
      `<tr><td>${esc(p.name)}</td><td>${fmt(p.x)}</td><td>${fmt(p.y)}</td><td>${fmt(p.theta_deg)}</td></tr>`).join('');
    const errs = pv.errors.map((e, i) =>
      `<tr><td>第 ${i + 1} 对</td><td>${fmt(e, 3)} mm</td><td>${e <= 1 ? '✓' : '✗ 超限'}</td></tr>`).join('');
    pvHTML = `
      <h2>预览结果（${typeName}）</h2>
      ${pv.ok
        ? '<div class="okbox">✓ 校验通过：对应点误差均 ≤ 1 mm，轮廓无内部重叠，可采纳。</div>'
        : `<div class="conflicts"><b>存在冲突，采纳将被拒绝：</b><ul>${pv.conflicts.map(c => `<li>${esc(c)}</li>`).join('')}</ul></div>`}
      <table class="info"><tr><th>碎片</th><th>x (mm)</th><th>y (mm)</th><th>角度 (°)</th></tr>${rows}</table>
      ${errs ? `<table class="info"><tr><th>对应点</th><th>误差</th><th>判定</th></tr>${errs}</table>` : ''}`;
  }
  const canAct = R.markersA.length >= 2 && R.markersA.length === R.markersB.length && R.aId && R.bId;
  return `
    <h2>新建拼接</h2>
    <label class="lab">碎片甲</label><select id="selA">${opts(R.aId)}</select>
    <canvas class="mini" id="miniA"></canvas>
    <div class="row"><span class="small">标记 ${R.markersA.length} 处</span><span class="spacer"></span>
      <button class="act" id="undoA" ${R.markersA.length ? '' : 'disabled'}>撤销标记</button></div>
    <label class="lab">碎片乙</label><select id="selB">${opts(R.bId)}</select>
    <canvas class="mini" id="miniB"></canvas>
    <div class="row"><span class="small">标记 ${R.markersB.length} 处</span><span class="spacer"></span>
      <button class="act" id="undoB" ${R.markersB.length ? '' : 'disabled'}>撤销标记</button></div>
    <div class="row" style="margin-top:10px">
      <button class="act" id="btnPreview" ${canAct ? '' : 'disabled'}>预 览</button>
      <button class="act primary" id="btnAccept" ${canAct ? '' : 'disabled'}>采 纳</button>
      <button class="act" id="btnAddTrial" ${canAct ? '' : 'disabled'}>加入试拼</button>
      <button class="act" id="btnClearMarkers">清空标记</button>
    </div>
    <div class="small" style="margin-top:4px">需每片 ≥2 处标记且数量一致；按序号一一对应。「加入试拼」只把该建立关系排入装配页的有序试拼序列，不落库。</div>
    ${pvHTML}`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------------------------------------------------------------- 轮廓修订面板
function reviseRelations() {
  return S.relations.filter(r => r.active &&
    (r.fragment_a === S.revise.fid || r.fragment_b === S.revise.fid))
    .sort((a, b) => a.id - b.id);
}

function reviseOtherName(r) {
  const otherId = r.fragment_a === S.revise.fid ? r.fragment_b : r.fragment_a;
  return fragById(otherId)?.name || `#${otherId}`;
}

function reviseRequiredCount(r) {
  return r.fragment_a === S.revise.fid ? r.markers_b.length : r.markers_a.length;
}

function revisePanelHTML() {
  const R = S.revise;
  const f = fragById(R.fid);
  if (!f) { S.revise = null; switchMode('view'); return ''; }
  const inc = reviseRelations();
  const relCards = inc.map(r => {
    const need = reviseRequiredCount(r);
    const have = (R.relMarkers[r.id] || []).length;
    return `
      <div class="card">
        <div class="row"><b>关系 #${r.id}</b> 与「${esc(reviseOtherName(r))}」
          <span class="spacer"></span>
          <span class="small ${have === need ? 'oktxt' : 'errtxt'}">${have} / ${need} 处</span>
          <button class="act" data-rev-undo-rel="${r.id}" ${have ? '' : 'disabled'}>撤销标记</button>
        </div>
        <canvas class="mini" data-rev-mini="${r.id}"></canvas>
        <div class="small">本片修订轮廓（棕）+ 新边缘标记（红）；点击边缘吸附放置，按序号与另一端 ${need} 处一一对应</div>
      </div>`;
  }).join('');

  const pv = R.preview;
  let pvHTML = '';
  if (pv) {
    const rows = pv.relations.map(rw => {
      const errs = rw.errors.length
        ? rw.errors.map((e, i) => `第 ${i + 1} 对 ${fmt(e, 3)} mm ${e <= 1 ? '✓' : '✗'}`).join('；')
        : '（标记数量不符，未计算）';
      const tag = rw.revised ? '已更新标记' : '仅位姿核验';
      return `<tr><td>#${rw.relation_id}（${tag}）</td><td>${esc(errs)}</td>
              <td>${rw.max_error_mm == null ? '-' : fmt(rw.max_error_mm, 3) + ' mm'}</td>
              <td>${rw.conflicts.length ? `<span class="errtxt">${esc(rw.conflicts.join('；'))}</span>` : '✓'}</td></tr>`;
    }).join('');
    const ov = pv.overlaps.map(o =>
      `<li>碎片「${esc(o.name_a)}」与「${esc(o.name_b)}」内部重叠 ${fmt(o.area_mm2, 2)} mm²</li>`).join('');
    pvHTML = `
      <h2>预览结果（不落库）</h2>
      ${pv.ok
        ? `<div class="okbox">✓ 校验通过：受影响组 ${esc(pv.involved_fragment_ids.join(', '))}
            对应点误差均 ≤ 1 mm、轮廓无内部重叠；确认后仅更新轮廓/标记/误差，
            位姿与分组不变，版本 v${S.version} → v${S.version + 1}。</div>`
        : `<div class="conflicts"><b>存在冲突，确认将被整次拒绝：</b>
            <ul>${pv.conflicts.map(c => `<li>${esc(c)}</li>`).join('')}</ul></div>`}
      ${rows ? `<table class="info"><tr><th>关系</th><th>逐对对应点误差</th><th>最大误差</th><th>判定</th></tr>${rows}</table>` : ''}
      ${ov ? `<div class="conflicts"><b>轮廓内部重叠：</b><ul>${ov}</ul></div>` : ''}`;
  }

  const enoughPts = R.points.length >= 3;
  const markersReady = inc.every(r =>
    (R.relMarkers[r.id] || []).length === reviseRequiredCount(r));
  const canPreview = enoughPts && markersReady;
  return `
    <h2>修订轮廓 #${f.id} ${esc(f.name)}</h2>
    <div class="small">基于装配版本 <b>v${S.version}</b> · 保持现有位姿 / 关系 / 分组不变 ·
      确认后装配版本只 +1${pv ? ' · <span class="errtxt">预览后请先确认；继续编辑会使预览失效</span>' : ''}</div>
    <label class="lab">修订轮廓顶点（${R.points.length} 个，毫米，主画布单击添加）</label>
    <div class="row">
      <button class="act" id="revUndoPt" ${R.points.length ? '' : 'disabled'}>撤销顶点</button>
      <button class="act" id="revClearPts" ${R.points.length ? '' : 'disabled'}>清空</button>
      <button class="act" id="revResetPts">重置为现轮廓</button>
    </div>
    <textarea id="revJson" rows="3" placeholder='粘贴修订轮廓 JSON，如 [[0,0],[40,0],[35,25],[10,30]]'></textarea>
    <div class="row"><button class="act" id="revLoadJson">载入坐标</button></div>
    <h2>新边缘标记（${inc.length} 条有效关系）</h2>
    ${relCards || '<div class="small">独立碎片：无有效拼接关系，只需通过轮廓校验。</div>'}
    ${inc.some(r => (R.relMarkers[r.id] || []).length !== reviseRequiredCount(r))
      ? '<div class="small errtxt">每条有效关系都必须提交新标记，数量与另一端一致（≥2 处、同片不重合）。</div>' : ''}
    <div class="row" style="margin-top:10px">
      <button class="act" id="btnRevPreview" ${canPreview ? '' : 'disabled'}>预 览</button>
      <button class="act primary" id="btnRevConfirm" ${pv && pv.ok ? '' : 'disabled'}>确认修订</button>
      <button class="act danger" id="btnRevCancel">取消</button>
    </div>
    ${pvHTML}`;
}

// ---------------------------------------------------------------- 面板事件
function bindPanelEvents() {
  document.querySelectorAll('[data-del-frag]').forEach(b => b.onclick = () => deleteFragment(+b.dataset.delFrag));
  document.querySelectorAll('[data-calib-frag]').forEach(b =>
    b.onclick = () => startCalibration(+b.dataset.calibFrag));
  document.querySelectorAll('[data-revise-frag]').forEach(b =>
    b.onclick = () => startRevision(+b.dataset.reviseFrag));
  document.querySelectorAll('[data-undo]').forEach(b => b.onclick = () => undoRelation(+b.dataset.undo));
  document.querySelectorAll('[data-trial-undo-rel]').forEach(b =>
    b.onclick = () => addTrialStep({ action: 'undo', relation_id: +b.dataset.trialUndoRel }));
  document.querySelectorAll('[data-trial-del]').forEach(b =>
    b.onclick = () => removeTrialStep(+b.dataset.trialDel));
  document.querySelectorAll('[data-hist-view]').forEach(b =>
    b.onclick = () => viewVersion(+b.dataset.histView));
  document.querySelectorAll('[data-hist-restore]').forEach(b =>
    b.onclick = () => previewRestore(+b.dataset.histRestore));
  const btnHistBack = document.getElementById('btnHistBack');
  if (btnHistBack) btnHistBack.onclick = closeHistory;
  const btnHistRestore = document.getElementById('btnHistRestore');
  if (btnHistRestore) btnHistRestore.onclick = () => previewRestore(S.history.view.version);
  const btnHistConfirm = document.getElementById('btnHistConfirm');
  if (btnHistConfirm) btnHistConfirm.onclick = confirmRestore;
  const btnClear = document.getElementById('btnTrialClear');
  if (btnClear) btnClear.onclick = clearTrial;
  const btnCommit = document.getElementById('btnTrialCommit');
  if (btnCommit) btnCommit.onclick = commitTrial;
  const btnCalibPreview = document.getElementById('btnCalibPreview');
  if (btnCalibPreview) btnCalibPreview.onclick = doCalibrationPreview;
  const btnCalibConfirm = document.getElementById('btnCalibConfirm');
  if (btnCalibConfirm) btnCalibConfirm.onclick = doCalibrationConfirm;
  const btnCalibClose = document.getElementById('btnCalibClose');
  if (btnCalibClose) btnCalibClose.onclick = closeCalibration;
  const btnTransferExport = document.getElementById('btnTransferExport');
  if (btnTransferExport) btnTransferExport.onclick = exportPackage;
  const btnTransferPreview = document.getElementById('btnTransferPreview');
  if (btnTransferPreview) btnTransferPreview.onclick = previewImport;
  const btnTransferConfirm = document.getElementById('btnTransferConfirm');
  if (btnTransferConfirm) btnTransferConfirm.onclick = confirmImport;
  const btnTransferClose = document.getElementById('btnTransferClose');
  if (btnTransferClose) btnTransferClose.onclick = closeTransferPreview;

  if (S.mode === 'draw') {
    $('#fragName').oninput = e => { S.draw.name = e.target.value; };
    $('#undoPt').onclick = () => { S.draw.points.pop(); renderPanel(); renderMain(); };
    $('#clearPts').onclick = () => { S.draw.points = []; renderPanel(); renderMain(); };
    $('#cancelDraw').onclick = () => switchMode('view');
    $('#saveFrag').onclick = saveFragment;
    $('#loadJson').onclick = () => {
      try {
        const arr = JSON.parse($('#jsonPts').value);
        if (!Array.isArray(arr) || arr.some(p => !Array.isArray(p) || p.length < 2)) throw 0;
        S.draw.points = arr.map(p => [+p[0], +p[1]]);
        renderPanel(); renderMain();
      } catch { setMessage('坐标 JSON 格式不正确', 'err'); }
    };
  }

  if (S.mode === 'relate') {
    const R = S.relate;
    $('#selA').onchange = e => { R.aId = +e.target.value; R.markersA = []; R.preview = null; renderPanel(); renderMain(); };
    $('#selB').onchange = e => { R.bId = +e.target.value; R.markersB = []; R.preview = null; renderPanel(); renderMain(); };
    $('#undoA').onclick = () => { R.markersA.pop(); R.preview = null; renderPanel(); renderMain(); };
    $('#undoB').onclick = () => { R.markersB.pop(); R.preview = null; renderPanel(); renderMain(); };
    $('#btnClearMarkers').onclick = () => { R.markersA = []; R.markersB = []; R.preview = null; renderPanel(); renderMain(); };
    $('#btnPreview').onclick = doPreview;
    $('#btnAccept').onclick = doAccept;
    $('#btnAddTrial').onclick = addRelateToTrial;
    bindMini($('#miniA'), 'A');
    bindMini($('#miniB'), 'B');
  }

  if (S.mode === 'revise') {
    const R = S.revise;
    $('#revUndoPt').onclick = () => { R.points.pop(); R.preview = null; renderPanel(); renderMain(); };
    $('#revClearPts').onclick = () => { R.points = []; R.preview = null; renderPanel(); renderMain(); };
    $('#revResetPts').onclick = () => {
      R.points = fragById(R.fid).contour.map(p => [+p[0], +p[1]]);
      R.preview = null; renderPanel(); renderMain();
    };
    $('#revLoadJson').onclick = () => {
      try {
        const arr = JSON.parse($('#revJson').value);
        if (!Array.isArray(arr) || arr.some(p => !Array.isArray(p) || p.length < 2)) throw 0;
        R.points = arr.map(p => [+p[0], +p[1]]);
        R.preview = null;
        renderPanel(); renderMain();
      } catch { setMessage('坐标 JSON 格式不正确', 'err'); }
    };
    $('#btnRevPreview').onclick = doRevisionPreview;
    $('#btnRevConfirm').onclick = doRevisionConfirm;
    $('#btnRevCancel').onclick = () => { S.revise = null; switchMode('view'); };
    document.querySelectorAll('[data-rev-undo-rel]').forEach(b =>
      b.onclick = () => {
        const rid = +b.dataset.revUndoRel;
        R.relMarkers[rid].pop();
        R.preview = null;
        renderPanel(); renderMain();
      });
  }
}

// ---------------------------------------------------------------- 业务动作
async function saveFragment() {
  const name = S.draw.name.trim() || `碎片${Date.now() % 10000}`;
  try {
    await api('/api/fragments', {
      method: 'POST',
      body: JSON.stringify({ name, contour: S.draw.points }),
    });
    setMessage(`碎片「${name}」已保存`);
    S.draw = { name: '', points: [] };
    await loadAssembly();
    switchMode('view');
  } catch (err) {
    setMessage(detailText(err.detail), 'err');   // 轮廓自交等：整次拒绝
  }
}

async function doPreview() {
  const R = S.relate;
  try {
    R.preview = await api('/api/relations/preview', {
      method: 'POST',
      body: JSON.stringify({
        fragment_a: R.aId, fragment_b: R.bId,
        markers_a: R.markersA, markers_b: R.markersB,
      }),
    });
    renderPanel(); renderMain();
  } catch (err) {
    setMessage(detailText(err.detail), 'err');
  }
}

async function doAccept() {
  const R = S.relate;
  try {
    const res = await api('/api/relations', {
      method: 'POST',
      body: JSON.stringify({
        fragment_a: R.aId, fragment_b: R.bId,
        markers_a: R.markersA, markers_b: R.markersB,
        expected_version: S.version,
      }),
    });
    setMessage(`拼接 #${res.relation_id} 已采纳`);
    S.relate = { aId: null, bId: null, markersA: [], markersB: [], preview: null };
    applyAssembly(res.assembly);
    switchMode('view');
  } catch (err) {
    if (err.status === 409) return handleConflict(err);          // 过期版本
    if (err.detail && err.detail.conflicts) {                    // 校验拒绝：展示冲突
      R.preview = { ok: false, relation_type: null, conflicts: err.detail.conflicts,
                    errors: [], placements: [], markers_world: null };
      renderPanel(); renderMain();
    }
    setMessage(detailText(err.detail), 'err');
  }
}

function addRelateToTrial() {
  const R = S.relate;
  // 同序列内唯一 client_id，供后续撤销步引用本步新建的关系
  const cid = `s${Date.now().toString(36)}${S.trial.steps.length}`;
  addTrialStep({
    action: 'relate',
    fragment_a: R.aId, fragment_b: R.bId,
    markers_a: R.markersA, markers_b: R.markersB,
    client_id: cid,
  });
  R.markersA = []; R.markersB = []; R.preview = null;
  setMessage('已把建立关系加入试拼序列，可在「查看装配」页确认');
  switchMode('view');
}

async function undoRelation(rid) {
  try {
    const res = await api(`/api/relations/${rid}/undo`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: S.version }),
    });
    setMessage(`拼接关系 #${rid} 已撤销，连通组已重算`);
    applyAssembly(res.assembly);
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

async function deleteFragment(fid) {
  const f = fragById(fid);
  if (!confirm(`确认删除碎片「${f?.name}」？其相关拼接关系将一并移除。`)) return;
  try {
    const res = await api(`/api/fragments/${fid}?expected_version=${S.version}`, { method: 'DELETE' });
    setMessage(`碎片「${f?.name}」已删除`);
    applyAssembly(res.assembly);
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

// ---------------------------------------------------------------- 轮廓修订
function startRevision(fid) {
  const f = fragById(fid);
  if (!f) return;
  const relMarkers = {};
  for (const r of reviseRelationsFor(fid)) {
    // 预填本片现有标记（来自当前轮廓），修复师可直接在此基础上改
    relMarkers[r.id] = (r.fragment_a === fid ? r.markers_a : r.markers_b)
      .map(p => [+p[0], +p[1]]);
  }
  S.revise = {
    fid,
    points: f.contour.map(p => [+p[0], +p[1]]),
    relMarkers,
    preview: null,
  };
  switchMode('revise');
}

function reviseRelationsFor(fid) {
  return S.relations.filter(r => r.active &&
    (r.fragment_a === fid || r.fragment_b === fid)).sort((a, b) => a.id - b.id);
}

function revisionPayload() {
  const R = S.revise;
  return {
    contour: R.points,
    relations: reviseRelations().map(r => ({
      relation_id: r.id,
      markers: R.relMarkers[r.id] || [],
    })),
    expected_version: S.version,
  };
}

async function doRevisionPreview() {
  const R = S.revise;
  try {
    R.preview = await api(`/api/fragments/${R.fid}/revision/preview`, {
      method: 'POST',
      body: JSON.stringify(revisionPayload()),
    });
    renderPanel(); renderMain();
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

async function doRevisionConfirm() {
  const R = S.revise;
  if (!(R.preview && R.preview.ok)) return;
  try {
    const res = await api(`/api/fragments/${R.fid}/revision/confirm`, {
      method: 'POST',
      body: JSON.stringify(revisionPayload()),
    });
    setMessage(`碎片 #${R.fid} 轮廓修订已确认，装配版本 v${res.version}（位姿、关系与分组不变）`);
    S.revise = null;
    S.trial = { steps: [], rehearsal: null, busy: false };  // 版本已变，旧试拼序列作废
    applyAssembly(res.assembly);
    switchMode('view');
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    if (err.detail && err.detail.conflicts) {   // 确认时重新校验失败：展示同一输入结果
      R.preview = {
        ok: false,
        relations: err.detail.relations || [],
        overlaps: err.detail.overlaps || [],
        conflicts: err.detail.conflicts || [],
        involved_fragment_ids: err.detail.involved_fragment_ids || [],
      };
      renderPanel(); renderMain();
    }
    setMessage(detailText(err.detail), 'err');
  }
}

// ---------------------------------------------------------------- 锚点整体校准
function startCalibration(fid) {
  const f = fragById(fid);
  if (!f) return;
  if (f.group_id == null) {
    setMessage(`碎片 #${fid}「${f.name}」是独立碎片（所在组不足两片），无法整体校准`, 'err');
    return;
  }
  S.calibrate = { anchorId: fid, preview: null };
  renderPanel(); renderMain();
}

async function doCalibrationPreview() {
  const C = S.calibrate;
  if (!C) return;
  try {
    C.preview = await api('/api/calibration/preview', {
      method: 'POST',
      body: JSON.stringify({ anchor_id: C.anchorId, expected_version: S.version }),
    });
    renderPanel(); renderMain();
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

async function doCalibrationConfirm() {
  const C = S.calibrate;
  if (!C || !C.preview || !C.preview.ok) return;
  if (C.preview.expected_version !== S.version) return;
  try {
    const res = await api('/api/calibration/confirm', {
      method: 'POST',
      body: JSON.stringify({ anchor_id: C.anchorId, expected_version: S.version }),
    });
    setMessage(`已以锚点 #${C.anchorId} 完成整体校准，装配版本 v${res.version}`
      + '（锚点位姿、轮廓、标记、关系状态与分组不变）');
    S.calibrate = null;
    S.trial = { steps: [], rehearsal: null, busy: false };  // 版本已变，旧试拼序列作废
    applyAssembly(res.assembly);
  } catch (err) {
    if (err.status === 409) return handleConflict(err);
    setMessage(detailText(err.detail), 'err');
  }
}

function closeCalibration() {
  S.calibrate = null;
  renderPanel(); renderMain();
}

function applyAssembly(a) {
  S.version = a.version;
  S.fragments = a.fragments;
  S.groups = a.groups;
  S.relations = a.relations;
  S.history.view = null;
  S.history.restore = null;
  S.calibrate = null;
  $('#versionBadge').textContent = `版本 v${S.version}`;
  renderPanel(); renderMain();
  loadVersions();
}

// ---------------------------------------------------------------- 小图（局部坐标 + 标记捕捉）
function fitTransform(contour, w, h, margin = 18) {
  const xs = contour.map(p => p[0]), ys = contour.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const s = Math.min((w - 2 * margin) / Math.max(maxX - minX, 1e-6),
                     (h - 2 * margin) / Math.max(maxY - minY, 1e-6));
  return {
    s,
    ox: (w - s * (maxX - minX)) / 2 - s * minX,
    oy: (h - s * (maxY - minY)) / 2 + s * maxY,
  };
}

function projectOnSegment(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return [a[0] + t * dx, a[1] + t * dy];
}

function snapToContour(p, contour) {
  let best = null, bd = Infinity;
  for (let i = 0; i < contour.length; i++) {
    const q = projectOnSegment(p, contour[i], contour[(i + 1) % contour.length]);
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (d < bd) { bd = d; best = q; }
  }
  return best;
}

function drawMini(cv, frag, markers) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  if (!frag) { c.fillStyle = '#999'; c.fillText('请选择碎片', 10, 20); return null; }
  const t = fitTransform(frag.contour, w, h);
  const path = new Path2D();
  frag.contour.forEach((p, i) => {
    const sx = p[0] * t.s + t.ox, sy = -p[1] * t.s + t.oy;
    i === 0 ? path.moveTo(sx, sy) : path.lineTo(sx, sy);
  });
  path.closePath();
  c.fillStyle = '#c89b5a33'; c.fill(path);
  c.strokeStyle = '#8a6d3b'; c.lineWidth = 1.5; c.stroke(path);
  markers.forEach((m, i) => {
    const sx = m[0] * t.s + t.ox, sy = -m[1] * t.s + t.oy;
    c.fillStyle = '#c0392b';
    c.beginPath(); c.arc(sx, sy, 4, 0, 7); c.fill();
    c.fillStyle = '#7b241c'; c.font = '10px sans-serif';
    c.fillText(`${i + 1}`, sx + 6, sy - 4);
  });
  return t;
}

function drawMinis() {
  const R = S.relate;
  const tA = drawMini($('#miniA'), fragById(R.aId), R.markersA);
  const tB = drawMini($('#miniB'), fragById(R.bId), R.markersB);
  $('#miniA')._t = tA; $('#miniB')._t = tB;
}

function drawRevisionMinis() {
  const R = S.revise;
  for (const r of reviseRelations()) {
    const cv = document.querySelector(`[data-rev-mini="${r.id}"]`);
    if (!cv) continue;
    if (R.points.length < 3) {
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth, h = cv.clientHeight;
      cv.width = w * dpr; cv.height = h * dpr;
      const c = cv.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);
      c.fillStyle = '#999'; c.font = '12px sans-serif';
      c.fillText('请先在主画布画出 ≥3 个修订轮廓顶点', 10, h / 2);
      cv._t = null;
      cv.onclick = () => setMessage('请先在主画布画出修订轮廓（≥3 个顶点）', 'err');
      continue;
    }
    const pseudo = { contour: R.points };
    cv._t = drawMini(cv, pseudo, R.relMarkers[r.id] || []);
    cv.onclick = e => {
      const t = cv._t;
      if (!t) { setMessage('请先在主画布画出修订轮廓（≥3 个顶点）', 'err'); return; }
      const rect = cv.getBoundingClientRect();
      const local = [(e.clientX - rect.left - t.ox) / t.s,
                     -(e.clientY - rect.top - t.oy) / t.s];
      const snapped = snapToContour(local, R.points);   // 新标记吸附到修订轮廓边缘
      if (!R.relMarkers[r.id]) R.relMarkers[r.id] = [];
      R.relMarkers[r.id].push(snapped);
      R.preview = null;
      renderPanel(); renderMain();
    };
  }
}

function bindMini(cv, side) {
  cv.onclick = e => {
    const R = S.relate;
    const frag = fragById(side === 'A' ? R.aId : R.bId);
    const t = cv._t;
    if (!frag || !t) return;
    const rect = cv.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const local = [(px - t.ox) / t.s, -(py - t.oy) / t.s];
    const snapped = snapToContour(local, frag.contour);   // 边缘标记：吸附到轮廓
    (side === 'A' ? R.markersA : R.markersB).push(snapped);
    R.preview = null;
    renderPanel(); renderMain();
  };
}

// ---------------------------------------------------------------- 主画布交互
let drag = null;
canvas.addEventListener('mousedown', e => {
  drag = { x: e.offsetX, y: e.offsetY, moved: false };
});
canvas.addEventListener('mousemove', e => {
  if (!drag) return;
  const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (drag.moved) {
    S.cam.ox += dx; S.cam.oy += dy;
    drag.x = e.offsetX; drag.y = e.offsetY;
    renderMain();
  }
});
canvas.addEventListener('mouseup', e => {
  const wasClick = drag && !drag.moved;
  drag = null;
  if (wasClick && S.mode === 'draw') {
    S.draw.points.push(toWorld(e.offsetX, e.offsetY).map(v => +v.toFixed(3)));
    renderPanel(); renderMain();
  }
  if (wasClick && S.mode === 'revise') {
    // 修订轮廓在碎片局部坐标系中编辑；主画布显示的是世界坐标，需逆位姿变换
    const f = fragById(S.revise.fid);
    const [wx, wy] = toWorld(e.offsetX, e.offsetY);
    const c = Math.cos(f.theta), s = Math.sin(f.theta);
    const lx = c * (wx - f.x) + s * (wy - f.y);
    const ly = -s * (wx - f.x) + c * (wy - f.y);
    S.revise.points.push([+lx.toFixed(3), +ly.toFixed(3)]);
    S.revise.preview = null;   // 轮廓一变，旧预览失效
    renderPanel(); renderMain();
  }
});
canvas.addEventListener('mouseleave', () => { drag = null; });
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const k = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  const [wx, wy] = toWorld(e.offsetX, e.offsetY);
  S.cam.scale = Math.min(200, Math.max(0.2, S.cam.scale * k));
  S.cam.ox = e.offsetX - wx * S.cam.scale;
  S.cam.oy = e.offsetY + wy * S.cam.scale;
  renderMain();
}, { passive: false });

// ---------------------------------------------------------------- 模式切换
function switchMode(mode) {
  if (S.mode === 'revise' && mode !== 'revise') S.revise = null;  // 离开修订页即结束会话
  if (S.mode === 'view' && mode !== 'view') S.calibrate = null;   // 离开查看页即结束校准会话
  if (mode !== 'view') { S.history.view = null; S.history.restore = null; }
  S.mode = mode;
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));
  if (mode === 'relate' && S.relate.aId == null && S.fragments.length >= 2) {
    S.relate.aId = S.fragments[0].id;
    S.relate.bId = S.fragments[1].id;
  }
  renderPanel(); renderMain();
}
document.querySelectorAll('.mode-btn').forEach(b =>
  b.addEventListener('click', () => switchMode(b.dataset.mode)));

window.addEventListener('resize', renderMain);

// ---------------------------------------------------------------- 启动
loadAssembly().catch(err => setMessage('加载失败：' + detailText(err.detail), 'err'));
