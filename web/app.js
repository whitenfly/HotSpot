'use strict';

/* ============================================================
 * HotSpot 热点数据服务 — Web 控制台
 * 纯原生 JS，无任何依赖；所有请求走相对路径 /api/hotspot/*。
 * ============================================================ */

/* ---------------- 常量 ---------------- */
const API = '/api/hotspot';
const POLL_INTERVAL = 10000;                       // 自动刷新周期：10s

const TABS = ['raw', 'cleaned', 'ai', 'sources', 'config', 'prompts', 'manage'];
const DATA_TABS = ['raw', 'cleaned', 'ai', 'sources'];   // 参与自动刷新的数据标签
const TAB_LABEL = {
  raw: '原始数据',
  cleaned: '清洗后数据',
  ai: 'AI 整理结果',
  sources: '数据源可用性',
  config: '配置',
  prompts: '提示词',
  manage: '数据源',
};
// 各标签「内容容器」的后缀（用于空态时隐藏内容）
const STATE_SUFFIX = { raw: 'TableWrap', cleaned: 'TableWrap', ai: 'Content', sources: 'TableWrap', manage: 'List' };

/* ---------------- DOM 快捷方式 ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const byId = (id) => document.getElementById(id);

/* ---------------- 格式化辅助 ---------------- */

/** HTML 转义（所有来自接口的数据注入前必须经过此函数） */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function pad2(n) { return String(n).padStart(2, '0'); }

function fmtDate(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** 毫秒时间戳 → "YYYY-MM-DD HH:mm:ss"（本地时间）；兼容秒级时间戳与 ISO 字符串 */
function fmtTime(v) {
  if (v === null || v === undefined || v === '' || v === 0) return '—';
  const n = Number(v);
  if (Number.isFinite(n)) {
    let ms = n;
    if (n > 0 && n < 1e11) ms = n * 1000;          // 秒级时间戳 → 毫秒
    const d = new Date(ms);
    return isNaN(d.getTime()) ? String(v) : fmtDate(d);
  }
  const d = new Date(v);                            // 兼容 ISO 字符串
  return isNaN(d.getTime()) ? String(v) : fmtDate(d);
}

function fmtNum(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : String(v);
}

/** 热度 + 单位，如 "1,234 万" */
function fmtHeat(it) {
  if (!it) return '—';
  const h = it.heat;
  const u = it.heatUnit;
  if (h === null || h === undefined || h === '') return u ? String(u) : '—';
  return fmtNum(h) + (u ? ' ' + u : '');
}

/** 0-1 之间取值（AI 归一化热度 → 进度条宽度） */
function clamp01(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

/** 仅放行 http(s) 链接，防止异常数据注入 */
function safeUrl(u) {
  if (typeof u !== 'string') return null;
  const s = u.trim();
  return /^https?:\/\//i.test(s) ? s : null;
}

/** 接口返回的 items/total 可能是数量（数字）或数组，统一取数量 */
function countOf(v) {
  if (typeof v === 'number') return v;
  if (Array.isArray(v)) return v.length;
  return null;
}

/** runId 过长时截断展示 */
function shortId(id) {
  const s = String(id == null ? '' : id);
  return s.length > 16 ? s.slice(0, 14) + '…' : s;
}

/* ---------------- API 层 ---------------- */

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    throw new ApiError('网络请求失败（服务未启动或连接被拒绝）', 0, String(e));
  }
  if (!res.ok) {
    let msg = '';
    try {
      const text = await res.text();
      try {
        const j = JSON.parse(text);
        msg = j.error || j.detail || text;
      } catch (_){ msg = text; }
    } catch (_) { /* 读取失败则仅展示状态码 */ }
    throw new ApiError('HTTP ' + res.status + (msg ? '：' + String(msg).slice(0, 300) : ''), res.status, msg);
  }
  return res;
}

const getJson = (p) => request(p).then((r) => r.json());
const getText = (p) => request(p).then((r) => r.text());

/* ---------------- 全局状态 ---------------- */
const state = {
  tab: 'raw',
  autoRefresh: true,
  busy: false,                 // 控制类操作（fetch/clean/ai/run-all）进行中
  status: null,
  data: {},                    // tab -> 最近一次接口返回
  dirty: {},                   // tab -> 需要强制重新拉取
  sort: {
    raw: { key: null, dir: 1 },
    cleaned: { key: null, dir: 1 },
  },
  aiCategory: '',
  prompt: { file: null, apiName: null, original: '', dirty: false },
  manage: {
    editing: null,          // null=表单关闭；'new'=新增；其他=编辑中的源 id
    editorOriginal: '',     // 表单打开时的字段快照（取消时检测未保存修改）
    test: {},               // 源 id -> { loading, open, data, error, rawExpanded }
    fetch: {},              // 源 id -> { loading, data, error, sourceCount }
    filter: '',             // 领域筛选（'' = 全部）
    toggle: {},             // 源 id -> { loading }（启停切换请求进行中）
    rsshub: {
      instances: null,      // null=未加载；数组=已加载实例列表
      loading: false,       // 实例列表请求进行中
      error: null,          // 列表加载错误（无数据时行内展示）
      testing: {},          // 实例 url -> true（该实例测试进行中）
      testingAll: false,    // 「全部测试」顺序执行中
    },
  },
};
TABS.forEach((t) => { state.data[t] = null; state.dirty[t] = true; });

/* ---------------- 常用元素缓存 ---------------- */
const el = {
  statusBadge: byId('statusBadge'),
  stageChip: byId('stageChip'),
  connState: byId('connState'),
  statusMsg: byId('statusMsg'),
  timeInfo: byId('timeInfo'),
  runsInfo: byId('runsInfo'),
  lastError: byId('lastError'),
  forceChk: byId('forceChk'),
  autoChk: byId('autoChk'),
  btnFetch: byId('btnFetch'),
  btnClean: byId('btnClean'),
  btnAi: byId('btnAi'),
  btnRunAll: byId('btnRunAll'),
  banner: byId('errorBanner'),
  bannerText: byId('bannerText'),
  bannerClose: byId('bannerClose'),
  overlay: byId('overlay'),
  overlayText: byId('overlayText'),
  overlayHint: byId('overlayHint'),
  toastBox: byId('toastBox'),
  tabs: $$('.tab'),
};

/* ---------------- 全局反馈：横幅 / 轻提示 / 遮罩 ---------------- */

function showBanner(msg) {
  el.bannerText.textContent = msg;
  el.banner.hidden = false;
}

function hideBanner() { el.banner.hidden = true; }

function toast(msg, type = 'ok') {
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = msg;
  el.toastBox.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 350);
  }, 4200);
}

function showOverlay(text, hint) {
  el.overlayText.textContent = text;
  el.overlayHint.textContent = hint || '';
  el.overlay.hidden = false;
}

function hideOverlay() { el.overlay.hidden = true; }

/* ---------------- 服务状态 ---------------- */

async function refreshStatus(silent = true) {
  try {
    state.status = await getJson(API + '/status');
    el.connState.hidden = true;
    renderStatus();
  } catch (e) {
    el.connState.hidden = false;
    if (!silent) showBanner('获取服务状态失败：' + e.message);
  }
}

function renderStatus() {
  const s = state.status || {};
  const running = !!s.running;
  const err = s.lastError || null;

  // 运行状态徽章：运行中 / 空闲 / 错误
  const cls = running ? 'badge-ok' : (err ? 'badge-err' : 'badge-idle');
  const txt = running ? '运行中' : (err ? '错误' : '空闲');
  el.statusBadge.className = 'badge ' + cls;
  el.statusBadge.textContent = txt;

  el.stageChip.textContent = '阶段 ' + (s.stage || '—');
  el.statusMsg.textContent = s.message || (running ? '运行中…' : '空闲');

  let t = '';
  if (running && s.startedAt) t = '开始于 ' + fmtTime(s.startedAt);
  else if (s.finishedAt) t = '完成于 ' + fmtTime(s.finishedAt);
  el.timeInfo.textContent = t;

  // runs：阶段 -> 最近 runId
  const runs = s.runs || {};
  const parts = ['raw', 'cleaned', 'ai'].filter((k) => runs[k]);
  el.runsInfo.innerHTML = parts.length
    ? parts.map((k) =>
        '<span class="run-chip" title="' + esc(k) + ' 最近 runId">' + esc(k) +
        ':<span class="mono">' + esc(shortId(runs[k])) + '</span></span>').join('')
    : '';

  el.lastError.textContent = err ? '最近错误：' + err : '';
  el.lastError.hidden = !err;
}

/* ---------------- 标签页切换 ---------------- */

function activateTab(tab) {
  if (!TABS.includes(tab)) tab = 'raw';
  state.tab = tab;
  el.tabs.forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
  TABS.forEach((t) => byId('panel-' + t).classList.toggle('active', t === tab));
  if (('#' + tab) !== location.hash) history.replaceState(null, '', '#' + tab);
  loadTab(tab);
}

function loadTab(tab, opts = {}) {
  const fn = LOADERS[tab];
  if (!fn) return;
  fn(opts);
  // 命中缓存且开启自动刷新时，后台静默更新（stale-while-revalidate）
  if (!opts.force && state.autoRefresh && DATA_TABS.includes(tab) &&
      state.data[tab] && !state.dirty[tab]) {
    fn({ force: true, silent: true });
  }
}

/* ---------------- 通用标签数据加载器 ---------------- */

/**
 * 生成某个标签的加载函数：带缓存 / 脏标记 / 404 友好空态。
 * @param {string} tab   标签名（同时是接口路径最后一段）
 * @param {Function} render 渲染函数（数据取 state.data[tab]）
 */
function makeLoader(tab, render) {
  return async function load(opts = {}) {
    const { force = false, silent = false } = opts;
    if (state.busy) return;                          // 控制操作进行中，暂停数据加载
    const hasData = state.data[tab] != null;
    if (!force && hasData && !state.dirty[tab]) { render(); return; }

    if (!silent || !hasData) showTabState(tab, 'loading');
    try {
      state.data[tab] = await getJson(API + '/' + tab);
      state.dirty[tab] = false;
      render();
    } catch (e) {
      if (e.status === 404) {
        // 404 = 尚无数据：展示服务端给出的友好提示，而非报错
        state.data[tab] = null;
        state.dirty[tab] = false;
        showTabState(tab, 'empty', e.body || '暂无数据');
      } else {
        if (!hasData) showTabState(tab, 'error', e.message);
        if (!silent) showBanner('加载' + TAB_LABEL[tab] + '失败：' + e.message);
      }
    }
  };
}

const LOADERS = {
  raw: makeLoader('raw', () => renderSnapshot('raw')),
  cleaned: makeLoader('cleaned', () => renderSnapshot('cleaned')),
  ai: makeLoader('ai', renderAi),
  sources: makeLoader('sources', renderSources),
  config: makeLoader('config', renderConfig),
  prompts: makeLoader('prompts', renderPromptsList),
  manage: loadManage,
};

/** 标签内容区状态（加载中 / 空 / 错误）；config 与 prompts 无状态容器，自动跳过 */
function showTabState(tab, kind, msg = '') {
  const stateEl = byId(tab + 'State');
  if (!stateEl) return;
  const contentEl = byId(tab + (STATE_SUFFIX[tab] || ''));
  if (contentEl) contentEl.hidden = true;
  const statsEl = byId(tab + 'Stats');
  if (statsEl) statsEl.innerHTML = '';

  stateEl.hidden = false;
  stateEl.className = 'tab-state' + (kind === 'error' ? ' tab-state-error' : '');
  if (kind === 'loading') {
    stateEl.innerHTML = '<div class="spinner" aria-hidden="true"></div><div>加载中…</div>';
  } else if (kind === 'empty') {
    stateEl.innerHTML =
      '<div class="empty-title">暂无' + esc(TAB_LABEL[tab]) + '</div>' +
      '<div class="empty-hint">' + esc(msg || '请先执行相应操作生成数据') + '</div>';
  } else {
    stateEl.innerHTML =
      '<div class="empty-title">加载失败</div>' +
      '<div class="empty-hint">' + esc(msg) + '</div>';
  }
}

/* ---------------- 原始 / 清洗后数据表 ---------------- */

function statHtml(k, v) {
  return '<span class="stat"><span class="stat-k">' + k + '</span>' +
    '<span class="stat-v">' + v + '</span></span>';
}

function renderSnapshot(kind) {
  const d = state.data[kind];
  const items = Array.isArray(d && d.items) ? d.items : [];
  const countLabel = kind === 'cleaned' ? '清洗条数' : '总条数';
  byId(kind + 'Stats').innerHTML = [
    statHtml(countLabel, fmtNum(items.length)),
    statHtml('获取时间', fmtTime(d && d.fetchedAt)),
    statHtml('runId', esc(String((d && d.runId) || '—'))),
    statHtml('数据源', fmtNum(d && d.sources && d.sources.length)),
  ].join('');
  renderItemsTable(kind, items);
}

function currentItems(kind) {
  const d = state.data[kind];
  return Array.isArray(d && d.items) ? d.items : [];
}

function renderItemsTable(kind, items) {
  if (!items.length) { showTabState(kind, 'empty', '暂无数据'); return; }
  byId(kind + 'State').hidden = true;
  byId(kind + 'TableWrap').hidden = false;
  updateSortIndicators(kind);
  byId(kind + 'Tbody').innerHTML = sortItems(kind, items).map(rowHtml).join('');
}

function rowHtml(it) {
  const src = it.sourceName || it.source || '—';
  const url = safeUrl(it.url);
  return '<tr>' +
    '<td class="td-trunc td-title" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</td>' +
    '<td class="td-trunc" title="' + esc(src) + '">' + esc(src) + '</td>' +
    '<td class="td-trunc" title="' + esc(it.domain == null ? '' : it.domain) + '">' + esc(it.domain || '—') + '</td>' +
    '<td class="td-num">' + esc(fmtHeat(it)) + '</td>' +
    '<td class="td-num">' + fmtTime(it.publishedAt) + '</td>' +
    '<td class="td-link">' + (url
      ? '<a class="link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>'
      : '<span class="faint">—</span>') + '</td>' +
    '</tr>';
}

/* ---- 列排序 ---- */

function cellValue(it, key) {
  if (key === 'source') return it.sourceName || it.source || '';
  return it[key];
}

function sortItems(kind, items) {
  const s = state.sort[kind];
  if (!s.key) return items;
  const numeric = s.key === 'heat' || s.key === 'publishedAt';
  return items.slice().sort((a, b) => {
    const va = cellValue(a, s.key);
    const vb = cellValue(b, s.key);
    if (numeric) return ((Number(va) || 0) - (Number(vb) || 0)) * s.dir;
    return String(va == null ? '' : va).localeCompare(String(vb == null ? '' : vb), 'zh') * s.dir;
  });
}

function bindSortHeaders(kind) {
  const thead = byId(kind + 'Table').querySelector('thead');
  thead.addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const key = th.dataset.key;
    const s = state.sort[kind];
    if (s.key === key) s.dir = -s.dir;
    else { s.key = key; s.dir = 1; }
    renderItemsTable(kind, currentItems(kind));
  });
}

function updateSortIndicators(kind) {
  const s = state.sort[kind];
  $$('#' + kind + 'Table th.sortable').forEach((th) => {
    const on = th.dataset.key === s.key;
    th.classList.toggle('sort-asc', on && s.dir === 1);
    th.classList.toggle('sort-desc', on && s.dir === -1);
  });
}

/* ---------------- AI 整理结果 ---------------- */

function renderAi() {
  const d = state.data.ai;
  const items = Array.isArray(d && d.items) ? d.items : [];
  const cats = Array.isArray(d && d.categories) ? d.categories : [];
  if (!items.length) { showTabState('ai', 'empty', '暂无 AI 整理结果'); return; }

  byId('aiState').hidden = true;
  byId('aiContent').hidden = false;
  byId('aiStats').innerHTML = [
    statHtml('总条数', fmtNum(d.total != null ? d.total : items.length)),
    statHtml('生成时间', fmtTime(d.generatedAt)),
    statHtml('模型', esc(String(d.model || '—'))),
    statHtml('原始条数', fmtNum(d.sourceItemCount)),
    statHtml('领域数', fmtNum(cats.length)),
  ].join('');

  // 总榜单：优先按 ranking.overall 的顺序
  const map = new Map(items.map((it) => [it.id, it]));
  const ranking = (d && d.ranking) || {};
  const overallIds = Array.isArray(ranking.overall) ? ranking.overall : [];
  const overall = overallIds.length
    ? overallIds.map((id) => map.get(id)).filter(Boolean)
    : items.slice().sort((a, b) => (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank));

  byId('aiOverall').innerHTML = overall.length
    ? overall.map((it, i) => aiRowHtml(it, it.rank != null ? it.rank : i + 1, false)).join('')
    : '<div class="tab-state"><div class="empty-title">榜单为空</div></div>';

  // 领域榜单：下拉选择（保留当前选择）
  const sel = byId('aiCatSelect');
  const prev = cats.includes(state.aiCategory) ? state.aiCategory : (cats[0] || '');
  state.aiCategory = prev;
  sel.innerHTML = cats.length
    ? cats.map((c) => '<option value="' + esc(c) + '"' + (c === prev ? ' selected' : '') + '>' + esc(c) + '</option>').join('')
    : '<option value="">（无分类）</option>';
  renderAiCategory();
}

function renderAiCategory() {
  const d = state.data.ai;
  const c = state.aiCategory;
  const items = Array.isArray(d && d.items) ? d.items : [];
  const map = new Map(items.map((it) => [it.id, it]));
  const ids = (c && d && d.ranking && Array.isArray(d.ranking[c])) ? d.ranking[c] : [];
  const list = ids.map((id) => map.get(id)).filter(Boolean);

  byId('aiCatList').innerHTML = list.length
    ? list.map((it, i) => {
        const rank = (it.categoryRanks && it.categoryRanks[c] != null) ? it.categoryRanks[c] : i + 1;
        return aiRowHtml(it, rank, true);
      }).join('')
    : '<div class="tab-state"><div class="empty-title">该领域暂无榜单数据</div></div>';
}

/** AI 已知热度标签（超出此列表的标签按 normal 灰色展示） */
const KNOWN_HEAT_LABELS = ['viral', 'top', 'hot', 'trending', 'normal'];

/**
 * AI 榜单条目。
 * @param {object} it      条目数据
 * @param {number} rank    展示名次
 * @param {boolean} compact 紧凑模式（领域榜单）
 */
function aiRowHtml(it, rank, compact) {
  const label = String(it.heatLabel || 'normal');
  let labelClass = label.toLowerCase().replace(/[^a-z0-9_-]/g, '');
  if (!KNOWN_HEAT_LABELS.includes(labelClass)) labelClass = 'normal';
  const heatNum = Number(it.heat);
  const pct = (clamp01(heatNum) * 100).toFixed(1);
  const heatTxt = Number.isFinite(heatNum) ? String(it.heat) : '—';
  const url = safeUrl(it.url);
  const rawTip = it.rawHeats ? esc(JSON.stringify(it.rawHeats)) : '';

  const titleHtml = url
    ? '<a class="rank-title" href="' + esc(url) + '" target="_blank" rel="noopener" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</a>'
    : '<span class="rank-title" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</span>';
  const linkHtml = url
    ? '<a class="link rank-link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>' : '';
  const bar = '<div class="progress" title="热度 ' + esc(heatTxt) +
    (rawTip ? ' · 原始热度 ' + rawTip : '') + '">' +
    '<div class="progress-fill heat-' + labelClass + '" style="width:' + pct + '%"></div></div>';

  let html = '<div class="rank-item' + (compact ? ' rank-compact' : '') + '">' +
    '<div class="rank-num' + (rank <= 3 ? ' top' : '') + '">' + esc(String(rank)) + '</div>' +
    '<div class="rank-body">' +
    '<div class="rank-line1">' + titleHtml +
    '<span class="badge heat-' + labelClass + '">' + esc(label) + '</span>' + linkHtml + '</div>' +
    '<div class="rank-heat">' + bar + '<span class="heat-val">' + esc(heatTxt) + '</span></div>';

  if (!compact) {
    const cats = (it.categories || []).map((c) => '<span class="chip">' + esc(c) + '</span>').join('');
    const srcs = (it.sourceNames || []).map((s) => '<span class="chip chip-src">' + esc(s) + '</span>').join('');
    html += '<div class="rank-chips">' + cats + srcs + '</div>';
    if (it.summary) {
      html += '<p class="rank-summary" title="' + esc(it.summary) + '">' + esc(it.summary) + '</p>';
    }
  }
  html += '</div></div>';
  return html;
}

/* ---------------- 数据源可用性 ---------------- */

function renderSources() {
  const d = state.data.sources;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  if (!list.length) {
    showTabState('sources', 'empty', '暂无数据源运行信息，请先执行「获取数据」');
    return;
  }
  byId('sourcesState').hidden = true;
  byId('sourcesTableWrap').hidden = false;

  const ok = list.filter((s) => s.connected === true).length;
  const total = list.length;
  const badgeCls = ok === total ? 'badge-ok' : (ok === 0 ? 'badge-err' : 'badge-warn');
  byId('sourcesStats').innerHTML =
    '<span class="badge ' + badgeCls + '">' + ok + '/' + total + ' 个源正常</span>' +
    statHtml('更新时间', fmtTime(d && d.updatedAt));

  byId('sourcesTbody').innerHTML = list.map((s) => {
    const name = s.sourceName || s.name || s.source || '—';
    const conn = s.connected === true
      ? '<span class="badge badge-ok">是</span>'
      : (s.connected === false ? '<span class="badge badge-err">否</span>' : '<span class="badge badge-idle">未知</span>');
    const skip = s.skipped ? ' <span class="chip chip-warn" title="本轮跳过（间隔未到或缓存命中）">跳过</span>' : '';
    const err = s.error
      ? '<span class="td-err-text" title="' + esc(s.error) + '">' + esc(s.error) + '</span>'
      : '<span class="faint">—</span>';
    return '<tr>' +
      '<td class="td-trunc td-title" title="' + esc(name) + '">' + esc(name) + skip + '</td>' +
      '<td class="td-conn">' + conn + '</td>' +
      '<td class="td-num">' + fmtNum(s.itemCount) + '</td>' +
      '<td class="td-num">' + fmtTime(s.fetchedAt) + '</td>' +
      '<td class="td-num">' + (s.durationMs != null ? fmtNum(s.durationMs) + ' ms' : '—') + '</td>' +
      '<td class="td-num">' + esc(fmtHeat(s)) + '</td>' +
      '<td class="td-trunc td-err">' + err + '</td>' +
      '</tr>';
  }).join('');
}

/* ---------------- 配置 ---------------- */

function renderConfig() {
  const c = state.data.config;
  if (!c) return;
  byId('configText').value = JSON.stringify(c, null, 2);
  const ai = c.ai || {};
  const rows = [
    ['baseUrl', ai.baseUrl],
    ['model', ai.model],
    ['maxTokens', ai.maxTokens],
    ['batchSize', ai.batchSize],
    ['contextWindow', ai.contextWindow],
  ];
  byId('configSummary').innerHTML = rows.map(([k, v]) =>
    '<tr><td class="kv-k">' + esc(k) + '</td><td class="kv-v">' + esc(v == null ? '—' : v) + '</td></tr>').join('');
}

async function saveConfig() {
  const ta = byId('configText');
  const btn = byId('configSave');
  const text = ta.value;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    showBanner('配置 JSON 格式错误，未保存：' + e.message);
    return;
  }
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.data.config = res.config || parsed;
    state.dirty.config = false;
    renderConfig();
    hideBanner();
    toast('配置已保存');
  } catch (e) {
    showBanner('保存配置失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

/* ---------------- 提示词 ---------------- */

/** 接口使用去掉 .md 后缀的文件名 */
function promptApiName(file) { return String(file).replace(/\.md$/i, ''); }

function renderPromptsList() {
  const d = state.data.prompts;
  const files = (d && Array.isArray(d.prompts)) ? d.prompts : [];
  const box = byId('promptList');
  if (!files.length) {
    box.innerHTML = '<div class="prompt-empty">暂无提示词文件</div>';
    return;
  }
  box.innerHTML = files.map((f) =>
    '<button type="button" class="prompt-item' + (state.prompt.file === f ? ' active' : '') +
    '" data-file="' + esc(f) + '" title="prompts/' + esc(f) + '">' +
    '<span class="prompt-name-text">' + esc(f) + '</span></button>').join('');
}

async function loadPromptFile(file) {
  if (state.prompt.dirty &&
      !window.confirm('「' + state.prompt.file + '」有未保存的修改，切换后将丢失。确定切换？')) {
    return;
  }
  const apiName = promptApiName(file);
  const ta = byId('promptText');
  ta.value = '加载中…';
  ta.disabled = true;
  try {
    const text = await getText(API + '/prompts/' + encodeURIComponent(apiName));
    state.prompt.file = file;
    state.prompt.apiName = apiName;
    state.prompt.original = text;
    state.prompt.dirty = false;
    ta.value = text;
    byId('promptName').textContent = file;
    updatePromptDirty();
    renderPromptsList();
  } catch (e) {
    ta.value = '';
    showBanner('加载提示词 ' + file + ' 失败：' + e.message);
  } finally {
    ta.disabled = false;
  }
}

async function savePrompt() {
  if (!state.prompt.apiName) {
    toast('请先在左侧选择一个提示词文件', 'warn');
    return;
  }
  const btn = byId('promptSave');
  const ta = byId('promptText');
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/prompts/' + encodeURIComponent(state.prompt.apiName), {
      method: 'PUT',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: ta.value,
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.prompt.original = ta.value;
    state.prompt.dirty = false;
    updatePromptDirty();
    hideBanner();
    toast('提示词已保存：' + state.prompt.file);
  } catch (e) {
    showBanner('保存提示词失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

function updatePromptDirty() {
  const dirty = byId('promptText').value !== state.prompt.original;
  state.prompt.dirty = dirty;
  byId('promptDirty').hidden = !dirty;
}

/* ---------------- 数据源管理 ---------------- */

/** 测试联通：原始响应预览的截断长度（字符） */
const RAW_PREVIEW_LIMIT = 2000;
/** 解析结果预览最多展示条数 */
const ITEMS_PREVIEW_LIMIT = 10;
/** 数据源 id 合法字符集（与后端校验一致） */
const SRC_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

async function loadManage(opts = {}) {
  const { force = false, silent = false } = opts;
  if (state.busy) return;
  loadRsshubInstances();                       // RSSHub 实例列表独立加载（内部缓存，失败互不影响）
  const hasData = state.data.manage != null;
  if (!force && hasData && !state.dirty.manage) { renderManage(); return; }
  if (!silent || !hasData) showTabState('manage', 'loading');
  try {
    state.data.manage = await getJson(API + '/sources/config');
    state.dirty.manage = false;
    renderSrcFilter();
    renderManage();
  } catch (e) {
    if (e.status === 404) {
      state.data.manage = null;
      state.dirty.manage = false;
      state.manage.filter = '';
      byId('srcFilterBar').hidden = true;
      showTabState('manage', 'empty', e.body || '暂无数据源配置');
    } else {
      if (!hasData) showTabState('manage', 'error', e.message);
      if (!silent) showBanner('加载数据源失败：' + e.message);
    }
  }
}

function findSource(id) {
  const d = state.data.manage;
  return (Array.isArray(d && d.sources) ? d.sources : []).find((s) => s.id === id) || null;
}

/** 数据源领域（空值归一为「综合」，与卡片徽章展示一致） */
function srcDomain(s) { return s.domain || '综合'; }

function renderManage() {
  const d = state.data.manage;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  const editorOpen = state.manage.editing != null;
  byId('srcEditor').hidden = !editorOpen;

  if (!list.length && !editorOpen) {
    showTabState('manage', 'empty', '点击右上角「新增数据源」创建第一个数据源');
    byId('srcFilterBar').hidden = true;
    return;
  }
  byId('manageState').hidden = true;
  byId('manageList').hidden = false;
  byId('srcFilterBar').hidden = !list.length;

  const enabled = list.filter((s) => s.enabled !== false).length;
  byId('manageStats').innerHTML = [
    statHtml('总源数', fmtNum(list.length)),
    statHtml('启用', fmtNum(enabled)),
  ].join('');

  // 领域筛选：仅过滤卡片网格，测试面板 / 编辑表单等其他状态不受影响
  const filter = state.manage.filter;
  const shown = filter ? list.filter((s) => srcDomain(s) === filter) : list;
  byId('srcFilterCount').textContent = '共 ' + shown.length + ' 个源';
  byId('manageList').innerHTML = shown.map(srcCardHtml).join('');
}

/** 重建领域筛选下拉选项（仅数据变化时调用）；当前筛选已失效时回退「全部」 */
function renderSrcFilter() {
  const d = state.data.manage;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  const domains = Array.from(new Set(list.map(srcDomain)))
    .sort((a, b) => a.localeCompare(b, 'zh'));
  if (state.manage.filter && !domains.includes(state.manage.filter)) state.manage.filter = '';
  const sel = byId('srcDomainFilter');
  sel.innerHTML = ['<option value="">全部</option>']
    .concat(domains.map((dm) => '<option value="' + esc(dm) + '">' + esc(dm) + '</option>'))
    .join('');
  sel.value = state.manage.filter;
}

function srcCardHtml(s) {
  const id = String(s.id == null ? '' : s.id);
  const enabled = s.enabled !== false;
  const t = state.manage.test[id] || null;
  const f = state.manage.fetch[id] || null;
  const g = state.manage.toggle[id] || null;
  const testing = !!(t && t.loading);
  const fetching = !!(f && f.loading);
  const toggling = !!(g && g.loading);
  const name = s.name == null ? id : s.name;

  const badges = [
    '<span class="badge src-type" title="数据源类型">' + esc(s.type || '—') + '</span>',
    '<span class="chip" title="领域">' + esc(srcDomain(s)) + '</span>',
  ];
  if (s.template) badges.push('<span class="badge badge-info" title="已配置解析模板">模板</span>');

  // 头部启停开关：点击即时切换（不打开编辑表单），请求期间禁用防连点
  const toggleHtml =
    '<label class="switch src-toggle' + (toggling ? ' switching' : '') + '" title="' +
      (enabled ? '点击禁用该数据源（即时生效）' : '点击启用该数据源（即时生效）') + '">' +
      '<input type="checkbox" data-act="toggle"' + (enabled ? ' checked' : '') + (toggling ? ' disabled' : '') + '>' +
      '<span class="switch-track"><span class="switch-knob"></span></span>' +
      '<span class="switch-label">' + (enabled ? '启用' : '禁用') + '</span>' +
    '</label>';

  return '<article class="src-card' + (t && t.open ? ' expanded' : '') + (enabled ? '' : ' is-disabled') +
    '" data-id="' + esc(id) + '">' +
    '<div class="src-card-head">' +
      '<div class="src-card-title">' +
        '<span class="src-name" title="' + esc(name) + '">' + esc(name) + '</span>' +
        badges.join('') +
      '</div>' +
      toggleHtml +
    '</div>' +
    '<div class="src-card-sub">' + esc(id) + '</div>' +
    '<div class="src-card-meta">' +
      '<span>条数上限 <span class="src-meta-v">' + fmtNum(s.limit) + '</span></span>' +
      '<span>最小间隔 <span class="src-meta-v">' + fmtNum(s.minIntervalMinutes) + '</span> 分钟</span>' +
      '<span>超时 <span class="src-meta-v">' + fmtNum(s.timeoutSeconds) + '</span> 秒</span>' +
      (s.description ? '<span class="src-meta-desc" title="' + esc(s.description) + '">' + esc(s.description) + '</span>' : '') +
    '</div>' +
    '<div class="src-card-actions">' +
      '<button type="button" class="btn btn-sm' + (testing ? ' loading' : '') + '" data-act="test"' + (testing ? ' disabled' : '') + '><span class="btn-spin" aria-hidden="true"></span><span>测试联通</span></button>' +
      '<button type="button" class="btn btn-sm' + (fetching ? ' loading' : '') + '" data-act="fetchone"' + (fetching ? ' disabled' : '') + '><span class="btn-spin" aria-hidden="true"></span><span>单独获取</span></button>' +
      '<button type="button" class="btn btn-sm" data-act="edit"><span>编辑</span></button>' +
      '<button type="button" class="btn btn-sm btn-danger" data-act="delete"><span class="btn-spin" aria-hidden="true"></span><span>删除</span></button>' +
    '</div>' +
    (f ? srcFetchHtml(f) : '') +
    (t && t.open ? srcTestHtml(t) : '') +
    '</article>';
}

function srcFetchHtml(f) {
  if (f.loading) {
    return '<div class="src-fetch-res is-loading"><span class="spinner" aria-hidden="true"></span>' +
      '<span>正在单独获取并并入最近快照…</span></div>';
  }
  if (f.error) {
    return '<div class="src-fetch-res is-err"><span class="badge badge-err">单独获取失败</span>' +
      '<span class="src-fetch-text">' + esc(f.error) + '</span></div>';
  }
  const d = f.data || {};
  return '<div class="src-fetch-res">' +
    '<span class="badge badge-ok">已并入快照</span>' +
    '<span class="src-fetch-text">合并后总条数 <span class="src-meta-v">' + fmtNum(d.items) + '</span>' +
    ' · 本源条数 <span class="src-meta-v">' + fmtNum(f.sourceCount) + '</span>' +
    ' · ' + (d.partial ? '部分合并（仅更新该源数据）' : '完整快照') + '</span></div>';
}

function srcTestHtml(t) {
  if (t.loading) {
    return '<div class="src-test"><div class="src-test-loading">' +
      '<span class="spinner" aria-hidden="true"></span><span>正在测试联通，抓取并解析中…</span></div></div>';
  }
  if (t.error) {
    return '<div class="src-test">' +
      '<div class="src-test-head"><span class="badge badge-err">✗ 测试失败</span>' +
      '<button type="button" class="btn btn-sm src-test-close" data-act="close-test">收起</button></div>' +
      '<div class="src-test-error">' + esc(t.error) + '</div></div>';
  }

  const d = t.data || {};
  const ok = d.connected === true;
  const raw = d.rawPreview == null ? '' : String(d.rawPreview);
  const over = raw.length > RAW_PREVIEW_LIMIT;
  const shown = (t.rawExpanded || !over) ? raw : raw.slice(0, RAW_PREVIEW_LIMIT);
  const total = Array.isArray(d.itemsPreview) ? d.itemsPreview.length : 0;
  const items = Array.isArray(d.itemsPreview) ? d.itemsPreview.slice(0, ITEMS_PREVIEW_LIMIT) : [];

  let html = '<div class="src-test">' +
    '<div class="src-test-head">' +
      (ok ? '<span class="badge badge-ok">✓ 联通成功</span>' : '<span class="badge badge-err">✗ 联通失败</span>') +
      '<span class="src-test-meta">' +
        '<span>耗时 <span class="src-meta-v">' + fmtNum(d.durationMs) + '</span> ms</span>' +
        '<span>获取于 ' + fmtTime(d.fetchedAt) + '</span>' +
        (d.template ? '<span>解析模板 <span class="src-meta-v">' + esc(d.template.type || '—') + '</span></span>' : '') +
      '</span>' +
      '<button type="button" class="btn btn-sm src-test-close" data-act="close-test">收起</button>' +
    '</div>';
  if (d.error) html += '<div class="src-test-error">' + esc(d.error) + '</div>';

  html += '<div class="src-sec">' +
    '<div class="src-sec-head"><span class="src-sec-title">原始响应预览</span>' +
      (over ? '<button type="button" class="btn btn-sm" data-act="toggle-raw">' +
        (t.rawExpanded ? '收起' : '展开全部（共 ' + fmtNum(raw.length) + ' 字符）') + '</button>' : '') +
    '</div>' +
    '<pre class="src-pre">' + (esc(shown) || '<span class="faint">（空响应）</span>') + '</pre>' +
    '</div>';

  html += '<div class="src-sec">' +
    '<div class="src-sec-head"><span class="src-sec-title">解析结果预览（共 ' + fmtNum(total) + ' 条' +
      (total > items.length ? '，展示前 ' + items.length + ' 条' : '') + '）</span></div>';
  if (!items.length) {
    html += '<div class="src-empty-mini">未解析出条目（请检查模板配置或响应格式）</div>';
  } else {
    html += '<div class="table-wrap table-flat src-items-wrap"><table><thead><tr>' +
      '<th class="td-title">标题</th><th>链接</th><th class="td-num">热度</th>' +
      '<th class="td-num">发布时间</th><th>摘要</th><th>extra</th>' +
      '</tr></thead><tbody>' + items.map(srcTestRowHtml).join('') + '</tbody></table></div>';
  }
  html += '</div></div>';
  return html;
}

function srcTestRowHtml(it) {
  const url = safeUrl(it && it.url);
  const extra = it && it.extra;
  const extraTxt = (extra == null || extra === '') ? '' :
    (typeof extra === 'string' ? extra : JSON.stringify(extra));
  return '<tr>' +
    '<td class="td-trunc td-title" title="' + esc(it && it.title) + '">' + esc((it && it.title) || '—') + '</td>' +
    '<td class="td-link">' + (url
      ? '<a class="link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>'
      : '<span class="faint td-trunc" title="' + esc(it && it.url) + '">' + esc((it && it.url) || '—') + '</span>') + '</td>' +
    '<td class="td-num">' + esc(fmtHeat(it)) + '</td>' +
    '<td class="td-num">' + fmtTime(it && it.publishedAt) + '</td>' +
    '<td class="td-trunc" title="' + esc(it && it.summary) + '">' + esc((it && it.summary) || '—') + '</td>' +
    '<td class="td-trunc td-extra mono" title="' + esc(extraTxt) + '">' +
      (extraTxt ? esc(extraTxt) : '<span class="faint">—</span>') + '</td>' +
    '</tr>';
}

async function testSource(id) {
  state.manage.test[id] = { loading: true, open: true, data: null, error: null, rawExpanded: false };
  renderManage();
  const t = state.manage.test[id];
  try {
    t.data = await request(API + '/sources/test/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }).then((r) => r.json());
  } catch (e) {
    t.error = e.message;
  } finally {
    t.loading = false;
    renderManage();
  }
}

async function fetchSource(id) {
  state.manage.fetch[id] = { loading: true, data: null, error: null, sourceCount: null };
  renderManage();
  const f = state.manage.fetch[id];
  try {
    const res = await request(API + '/sources/fetch/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 500);
    f.data = res;
    markDirty('raw', 'sources');
    // 单独获取接口不返回分源计数，从数据源可用性接口补取该源最新条数
    try {
      const avail = await getJson(API + '/sources');
      const st = (avail && Array.isArray(avail.sources) ? avail.sources : []).find((x) => x.source === id);
      f.sourceCount = st ? countOf(st.itemCount) : null;
    } catch (_) { /* 可用性查询失败不影响主结果展示 */ }
    toast('已并入最近快照，可继续执行数据清洗 / AI 整理');
  } catch (e) {
    f.error = e.message;
  } finally {
    f.loading = false;
    renderManage();
  }
}

async function deleteSource(id, btn) {
  const src = findSource(id);
  const name = src && src.name ? src.name : id;
  if (!window.confirm('确定删除数据源「' + name + '」（' + id + '）？删除后不可恢复。')) return;
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/sources/config/' + encodeURIComponent(id), { method: 'DELETE' })
      .then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    delete state.manage.test[id];
    delete state.manage.fetch[id];
    delete state.manage.toggle[id];
    if (state.manage.editing === id) state.manage.editing = null;
    toast('数据源已删除：' + id);
    await loadManage({ force: true });
  } catch (e) {
    showBanner('删除数据源失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

/** 卡片启停开关：POST toggle 即时切换启用状态，成功后同步本地 state 与编辑表单 */
async function toggleSource(id) {
  if (state.manage.toggle[id] && state.manage.toggle[id].loading) return;   // 防连点
  state.manage.toggle[id] = { loading: true };
  renderManage();
  try {
    const res = await request(API + '/sources/config/' + encodeURIComponent(id) + '/toggle', {
      method: 'POST',
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 500);
    const src = findSource(id);
    if (src) {
      src.enabled = res.enabled === true;
      toast((src.enabled ? '已启用 ' : '已禁用 ') + (src.name || id));
      // 编辑表单正开着该源时同步 enabled 复选框；表单原本干净则快照一并刷新，避免误报未保存
      if (state.manage.editing === id) {
        const wasClean = srcEditorSnapshot() === state.manage.editorOriginal;
        byId('srcEnabled').checked = src.enabled;
        if (wasClean) state.manage.editorOriginal = srcEditorSnapshot();
      }
    }
  } catch (e) {
    showBanner('切换启用状态失败：' + e.message);
  } finally {
    state.manage.toggle[id].loading = false;
    renderManage();
  }
}

/** 源卡片内启停开关的 change 事件委托（与 onManageListClick 的按钮委托并列） */
function onManageListChange(e) {
  const input = e.target.closest('input[data-act="toggle"]');
  if (!input || input.disabled) return;
  const card = input.closest('.src-card');
  const id = card ? card.dataset.id : '';
  if (id) toggleSource(id);
}

function srcEditorSnapshot() {
  return JSON.stringify([
    byId('srcId').value, byId('srcName').value, byId('srcType').value,
    byId('srcDomain').value, byId('srcEnabled').checked,
    byId('srcLimit').value, byId('srcInterval').value, byId('srcTimeout').value,
    byId('srcConfig').value, byId('srcTemplate').value,
  ]);
}

function openSrcEditor(id) {
  const isNew = id == null;
  const s = isNew ? null : findSource(id);
  if (!isNew && !s) { toast('数据源不存在：' + id, 'warn'); return; }
  state.manage.editing = isNew ? 'new' : id;

  byId('srcEditorTitle').textContent = isNew ? '新增数据源' : '编辑数据源';
  const idInput = byId('srcId');
  idInput.value = isNew ? '' : String(s.id);
  idInput.readOnly = !isNew;
  byId('srcName').value = isNew ? '' : (s.name || '');
  byId('srcType').value = isNew ? 'rss' : (s.type || 'rss');
  byId('srcDomain').value = isNew ? '综合' : (s.domain || '综合');
  byId('srcEnabled').checked = isNew ? true : s.enabled !== false;
  byId('srcLimit').value = isNew ? '30' : (s.limit == null ? '' : s.limit);
  byId('srcInterval').value = isNew ? '10' : (s.minIntervalMinutes == null ? '' : s.minIntervalMinutes);
  byId('srcTimeout').value = isNew ? '15' : (s.timeoutSeconds == null ? '' : s.timeoutSeconds);
  byId('srcConfig').value = JSON.stringify(isNew ? { url: '' } : (s.config || {}), null, 2);
  byId('srcTemplate').value = s && s.template ? JSON.stringify(s.template, null, 2) : '';
  state.manage.editorOriginal = srcEditorSnapshot();

  renderManage();
  byId('srcEditor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function cancelSrcEditor() {
  if (state.manage.editing != null && srcEditorSnapshot() !== state.manage.editorOriginal &&
      !window.confirm('表单有未保存的修改，确定放弃？')) return;
  state.manage.editing = null;
  renderManage();
}

async function saveSrcEditor() {
  const isNew = state.manage.editing === 'new';
  const editId = state.manage.editing;
  const btn = byId('srcSave');

  const id = byId('srcId').value.trim();
  if (isNew && !SRC_ID_RE.test(id)) {
    showBanner('数据源 id 仅允许字母 / 数字 / - / _，长度 1-64 位');
    byId('srcId').focus();
    return;
  }
  const name = byId('srcName').value.trim();
  if (!name) {
    showBanner('数据源名称不能为空');
    byId('srcName').focus();
    return;
  }

  let config;
  try {
    config = JSON.parse(byId('srcConfig').value || '{}');
  } catch (e) {
    showBanner('config 不是合法 JSON：' + e.message);
    return;
  }
  let template = null;
  const tplText = byId('srcTemplate').value.trim();
  if (tplText) {
    try {
      template = JSON.parse(tplText);
    } catch (e) {
      showBanner('template 不是合法 JSON：' + e.message);
      return;
    }
  }

  const payload = {
    id: isNew ? id : editId,
    name,
    type: byId('srcType').value,
    domain: byId('srcDomain').value.trim() || '综合',
    enabled: byId('srcEnabled').checked,
    config,
    template,
  };
  // 数字项留空时不提交，由服务端补默认值
  [['limit', 'srcLimit'], ['minIntervalMinutes', 'srcInterval'], ['timeoutSeconds', 'srcTimeout']]
    .forEach(([key, elId]) => {
      const v = byId(elId).value;
      const n = Number(v);
      if (v !== '' && Number.isFinite(n) && n >= 0) payload[key] = Math.round(n);
    });

  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const path = isNew ? API + '/sources/config' : API + '/sources/config/' + encodeURIComponent(editId);
    const res = await request(path, {
      method: isNew ? 'POST' : 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.manage.editing = null;
    hideBanner();
    toast(isNew ? '数据源已新增：' + id : '数据源已保存：' + editId);
    await loadManage({ force: true });
  } catch (e) {
    showBanner('保存数据源失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

function onManageListClick(e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn || btn.disabled) return;
  const card = btn.closest('.src-card');
  const id = card ? card.dataset.id : '';
  if (!id) return;
  const act = btn.dataset.act;
  if (act === 'test') testSource(id);
  else if (act === 'fetchone') fetchSource(id);
  else if (act === 'edit') openSrcEditor(id);
  else if (act === 'delete') deleteSource(id, btn);
  else if (act === 'close-test') {
    const t = state.manage.test[id];
    if (t) { t.open = false; renderManage(); }
  } else if (act === 'toggle-raw') {
    const t = state.manage.test[id];
    if (t) { t.rawExpanded = !t.rawExpanded; renderManage(); }
  }
}

/* ---------------- RSSHub 全局实例 ---------------- */

/** 实例列表加载（带缓存；force=true 时重新拉取），失败不影响源列表 */
async function loadRsshubInstances(opts = {}) {
  const { force = false } = opts;
  const r = state.manage.rsshub;
  if (r.loading) return;
  if (!force && Array.isArray(r.instances)) { renderRsshub(); return; }
  r.loading = true;
  renderRsshub();
  try {
    const d = await getJson(API + '/rsshub/instances');
    r.instances = Array.isArray(d && d.instances) ? d.instances : [];
    r.error = null;
  } catch (e) {
    // 已有旧列表时保留展示（横幅已提示），仅在无数据时行内展示错误
    r.error = Array.isArray(r.instances) ? null : e.message;
    showBanner('加载 RSSHub 实例列表失败：' + e.message);
  } finally {
    r.loading = false;
    renderRsshub();
  }
}

function renderRsshub() {
  const r = state.manage.rsshub;
  const list = Array.isArray(r.instances) ? r.instances : [];
  const hasView = r.loading || r.error || Array.isArray(r.instances);
  byId('rsshubPanel').hidden = !hasView;
  if (!hasView) return;

  const testAllBtn = byId('rsshubTestAll');
  const refreshBtn = byId('rsshubRefresh');
  testAllBtn.disabled = r.loading || r.testingAll || !list.length;
  testAllBtn.classList.toggle('loading', r.testingAll);
  refreshBtn.disabled = r.loading || r.testingAll;
  refreshBtn.classList.toggle('loading', r.loading);

  const tbody = byId('rsshubTbody');
  if (r.loading && !Array.isArray(r.instances)) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="rsshub-loading">' +
      '<span class="spinner" aria-hidden="true"></span>正在加载实例列表…</div></td></tr>';
    return;
  }
  if (!list.length) {
    tbody.innerHTML = r.error
      ? '<tr><td colspan="7"><div class="rsshub-error">加载实例列表失败：' + esc(r.error) + '</div></td></tr>'
      : '<tr><td colspan="7"><div class="src-empty-mini">暂无 RSSHub 实例</div></td></tr>';
    return;
  }
  tbody.innerHTML = list.map(rsshubRowHtml).join('');
}

function rsshubRowHtml(inst) {
  const r = state.manage.rsshub;
  const url = String(inst.url || '');
  const testing = !!r.testing[url];
  const onlineCls = inst.online === true ? 'badge-ok' : (inst.online === false ? 'badge-err' : 'badge-idle');
  const onlineTxt = inst.online === true ? '在线' : (inst.online === false ? '离线' : '未测');
  const errTxt = inst.online === false ? String(inst.lastError || '') : '';
  const dur = inst.durationMs != null ? fmtNum(inst.durationMs) + ' ms' : '—';
  const durTip = inst.statusCode != null ? 'HTTP ' + inst.statusCode : '';
  const href = safeUrl(url);

  return '<tr>' +
    '<td class="td-trunc td-title mono" title="' + esc(url) + '">' + (href
      ? '<a class="link" href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(url) + '</a>'
      : esc(url || '—')) + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.name == null ? '' : inst.name) + '">' + esc(inst.name || '—') + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.location == null ? '' : inst.location) + '">' + esc(inst.location || '—') + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.maintainer == null ? '' : inst.maintainer) + '">' + esc(inst.maintainer || '—') + '</td>' +
    '<td class="td-conn">' +
      '<span class="badge ' + onlineCls + '">' + onlineTxt + '</span>' +
      (errTxt ? '<div class="inst-err" title="' + esc(errTxt) + '">' + esc(errTxt) + '</div>' : '') +
    '</td>' +
    '<td class="td-num"' + (durTip ? ' title="' + esc(durTip) + '"' : '') + '>' + dur + '</td>' +
    '<td class="td-conn">' +
      '<button type="button" class="btn btn-sm' + (testing ? ' loading' : '') +
        '" data-act="test-inst" data-url="' + esc(url) + '"' +
        (testing || r.testingAll ? ' disabled' : '') + '>' +
        '<span class="btn-spin" aria-hidden="true"></span><span>测试</span></button>' +
    '</td>' +
    '</tr>';
}

/** 测试单个实例在线状态（url 以 encodeURIComponent 完整编码，含 https:// 前缀） */
async function testRsshubInstance(url) {
  const r = state.manage.rsshub;
  if (!url || r.testing[url]) return;
  r.testing[url] = true;
  renderRsshub();
  try {
    const res = await request(API + '/rsshub/test/' + encodeURIComponent(url), {
      method: 'POST',
    }).then((x) => x.json());
    const inst = (Array.isArray(r.instances) ? r.instances : []).find((x) => x.url === url);
    if (inst) {
      inst.online = res.online === true;
      inst.lastError = res.error || null;
      inst.durationMs = res.durationMs == null ? null : res.durationMs;
      inst.statusCode = res.statusCode == null ? null : res.statusCode;
    }
  } catch (e) {
    showBanner('测试 RSSHub 实例失败：' + e.message);
  } finally {
    delete r.testing[url];
    renderRsshub();
  }
}

/** 全部测试：顺序遍历实例，每完成一个即更新该行状态 */
async function testAllRsshubInstances() {
  const r = state.manage.rsshub;
  const list = Array.isArray(r.instances) ? r.instances.slice() : [];
  if (!list.length || r.testingAll) return;
  r.testingAll = true;
  renderRsshub();
  try {
    for (const inst of list) {
      await testRsshubInstance(String(inst.url || ''));
    }
    toast('RSSHub 实例测试完成：共 ' + list.length + ' 个');
  } finally {
    r.testingAll = false;
    renderRsshub();
  }
}

/** 实例表「测试」按钮的事件委托 */
function onRsshubTbodyClick(e) {
  const btn = e.target.closest('button[data-act="test-inst"]');
  if (!btn || btn.disabled) return;
  testRsshubInstance(btn.dataset.url);
}

/* ---------------- 控制操作（获取 / 清洗 / AI / 全流程） ---------------- */

function setControlsDisabled(dis) {
  [el.btnFetch, el.btnClean, el.btnAi, el.btnRunAll].forEach((b) => { b.disabled = dis; });
  el.forceChk.disabled = dis;
}

function markDirty() {
  for (const t of arguments) state.dirty[t] = true;
}

/**
 * 执行控制类操作：期间显示全屏遮罩并禁用全部控制按钮。
 * @param {HTMLElement} btn   触发按钮（显示行内 spinner）
 * @param {string} path       接口路径
 * @param {object} body       POST body
 * @param {string} label      操作名（用于错误提示）
 * @param {string} overlayText  遮罩主文案
 * @param {string} overlayHint  遮罩副文案
 * @param {Function} onOk     成功回调
 */
async function runControl(btn, path, body, label, overlayText, overlayHint, onOk) {
  if (state.busy) return;
  state.busy = true;
  setControlsDisabled(true);
  btn.classList.add('loading');
  showOverlay(overlayText, overlayHint);
  try {
    const res = await request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 500);
    onOk(res);
    hideBanner();
  } catch (e) {
    showBanner(label + '失败：' + e.message);
  } finally {
    state.busy = false;
    btn.classList.remove('loading');
    setControlsDisabled(false);
    hideOverlay();
    refreshStatus(true);
    if (DATA_TABS.includes(state.tab)) LOADERS[state.tab]({ force: true });
  }
}

/* ---------------- 自动刷新轮询 ---------------- */

function poll() {
  if (!state.autoRefresh || document.hidden) return;
  refreshStatus(true);                               // 控制操作期间也持续刷新状态
  if (!state.busy && DATA_TABS.includes(state.tab)) {
    LOADERS[state.tab]({ force: true, silent: true });
  }
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  // 控制按钮
  el.btnFetch.addEventListener('click', () => runControl(
    el.btnFetch, API + '/fetch', { force: el.forceChk.checked }, '获取数据',
    '正在获取数据…', '正在从各数据源抓取最新热点，请稍候',
    (res) => {
      markDirty('raw', 'sources');
      toast('获取完成：' + (countOf(res.items) != null ? countOf(res.items) + ' 条' : '完成'));
    }));
  el.btnClean.addEventListener('click', () => runControl(
    el.btnClean, API + '/clean', {}, '数据清洗',
    '正在清洗数据…', '正在对原始数据进行去重、合并与标准化…',
    (res) => {
      markDirty('cleaned');
      toast('清洗完成：' + (countOf(res.items) != null ? countOf(res.items) + ' 条' : '完成'));
    }));
  el.btnAi.addEventListener('click', () => runControl(
    el.btnAi, API + '/ai', {}, 'AI 整理',
    '正在 AI 整理…', 'AI 整理可能需要数分钟时间，请耐心等待，请勿关闭页面…',
    (res) => {
      markDirty('ai');
      toast('AI 整理完成：共 ' + (countOf(res.total) != null ? countOf(res.total) + ' 条' : '—'));
    }));
  el.btnRunAll.addEventListener('click', () => runControl(
    el.btnRunAll, API + '/run-all', { force: el.forceChk.checked }, '全流程',
    '正在执行全流程…', '依次执行「获取 → 清洗 → AI 整理」，可能需要数分钟…',
    () => {
      markDirty('raw', 'cleaned', 'ai', 'sources');
      toast('全流程执行完成');
    }));

  // 自动刷新开关
  el.autoChk.addEventListener('change', () => {
    state.autoRefresh = el.autoChk.checked;
    if (state.autoRefresh) poll();
  });

  // 标签锚点切换（浏览器前进 / 后退同样生效）
  window.addEventListener('hashchange', () => {
    const t = decodeURIComponent((location.hash || '').replace(/^#/, ''));
    if (TABS.includes(t) && t !== state.tab) activateTab(t);
  });

  // 各标签刷新按钮
  byId('rawRefresh').addEventListener('click', () => LOADERS.raw({ force: true }));
  byId('cleanedRefresh').addEventListener('click', () => LOADERS.cleaned({ force: true }));
  byId('aiRefresh').addEventListener('click', () => LOADERS.ai({ force: true }));
  byId('sourcesRefresh').addEventListener('click', () => LOADERS.sources({ force: true }));
  byId('configReload').addEventListener('click', () => LOADERS.config({ force: true }));
  byId('promptsReload').addEventListener('click', () => LOADERS.prompts({ force: true }));

  // AI 领域榜单选择
  byId('aiCatSelect').addEventListener('change', (e) => {
    state.aiCategory = e.target.value;
    renderAiCategory();
  });

  // 配置保存
  byId('configSave').addEventListener('click', saveConfig);

  // 提示词：列表点击 / 保存 / 编辑脏标记
  byId('promptList').addEventListener('click', (e) => {
    const b = e.target.closest('.prompt-item');
    if (b) loadPromptFile(b.dataset.file);
  });
  byId('promptSave').addEventListener('click', savePrompt);
  byId('promptText').addEventListener('input', updatePromptDirty);

  // 数据源管理：刷新 / 新增 / 表单保存取消 / 列表操作（事件委托）
  byId('manageRefresh').addEventListener('click', () => loadManage({ force: true }));
  byId('manageAdd').addEventListener('click', () => openSrcEditor(null));
  byId('srcSave').addEventListener('click', saveSrcEditor);
  byId('srcCancel').addEventListener('click', cancelSrcEditor);
  byId('manageList').addEventListener('click', onManageListClick);
  byId('manageList').addEventListener('change', onManageListChange);

  // 数据源管理：领域筛选（仅重渲染卡片网格，不动其他状态）
  byId('srcDomainFilter').addEventListener('change', (e) => {
    state.manage.filter = e.target.value;
    renderManage();
  });

  // RSSHub 全局实例：单行测试（委托）/ 全部测试 / 刷新列表
  byId('rsshubTbody').addEventListener('click', onRsshubTbodyClick);
  byId('rsshubTestAll').addEventListener('click', testAllRsshubInstances);
  byId('rsshubRefresh').addEventListener('click', () => loadRsshubInstances({ force: true }));

  // 错误横幅关闭
  el.bannerClose.addEventListener('click', hideBanner);

  // 回到页面时立即刷新一次
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.autoRefresh) poll();
  });
}

/* ---------------- 启动 ---------------- */

function init() {
  bindSortHeaders('raw');
  bindSortHeaders('cleaned');
  bindEvents();

  const h = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  activateTab(TABS.includes(h) ? h : 'raw');

  refreshStatus(false);
  setInterval(poll, POLL_INTERVAL);
}

init();
