/* fileconvert 前端逻辑 —— 零依赖原生 JS；所有请求用相对路径（本页挂在 /convert/ 下） */
'use strict';

const $ = (id) => document.getElementById(id);
const drop = $('drop'), fileInput = $('file'), fname = $('fname');
const targetCard = $('target-card'), targetsBox = $('targets');
const resultCard = $('result-card'), statusBox = $('status'), errorBox = $('error');

let currentFile = null;
let pollTimer = null;

const fmtSize = (n) => {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};

const setError = (msg) => {
  if (!msg) { errorBox.hidden = true; errorBox.textContent = ''; return; }
  errorBox.textContent = msg;
  errorBox.hidden = false;
};

/* ---------------- 第一步：选文件 ---------------- */
drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
['dragenter', 'dragover'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => {
  const f = e.dataTransfer?.files?.[0];
  if (f) acceptFile(f);
});
fileInput.addEventListener('change', () => {
  const f = fileInput.files?.[0];
  if (f) acceptFile(f);
});

async function acceptFile(file) {
  currentFile = file;
  setError('');
  resultCard.hidden = true;
  fname.innerHTML = '';
  fname.append('已选：');
  const b = document.createElement('b');
  b.textContent = file.name;
  fname.append(b, ` （${fmtSize(file.size)}）`);
  fname.hidden = false;

  const fd = new FormData();
  fd.append('filename', file.name);
  try {
    const r = await fetch('api/targets', { method: 'POST', body: fd, credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (!r.ok) { setError(d.detail || '这个格式暂时不支持。'); targetCard.hidden = true; return; }
    renderTargets(d);
  } catch (_) {
    setError('连不上服务器，稍后再试。');
  }
}

function renderTargets(d) {
  targetsBox.innerHTML = '';
  if (!d.targets.length) {
    setError(`暂时不支持转换 .${d.ext} 格式的文件。`);
    targetCard.hidden = true;
    return;
  }
  for (const t of d.targets) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tbtn';
    btn.textContent = t.key;
    btn.addEventListener('click', () => startConvert(t.ext, btn));
    targetsBox.append(btn);
  }
  targetCard.hidden = false;
  resultCard.hidden = true;
}

/* ---------------- 第二步：转换 ---------------- */
async function startConvert(target, btn) {
  if (!currentFile) return;
  setError('');
  document.querySelectorAll('.tbtn').forEach((b) => (b.disabled = true));
  btn.textContent = '上传中…';
  resultCard.hidden = false;
  statusBox.innerHTML = '<span class="st-running">正在上传…</span>';

  const fd = new FormData();
  fd.append('file', currentFile, currentFile.name);
  fd.append('target', target);

  try {
    const r = await fetch('api/convert', { method: 'POST', body: fd, credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (!r.ok) {
      setError(d.detail || '转换请求被拒绝。');
      statusBox.innerHTML = '';
      resetButtons();
      return;
    }
    // 上传完立刻在「已处理文件」里占一行，状态随后由 poll() 刷新
    upsertDone({ job: d.job, name: currentFile.name, from: extOf(currentFile.name),
                 to: target, status: 'queued', size: 0 });
    poll(d.job);
  } catch (_) {
    setError('上传失败，请检查网络后重试。');
    statusBox.innerHTML = '';
    resetButtons();
  }
}

function resetButtons() {
  document.querySelectorAll('.tbtn').forEach((b) => (b.disabled = false));
  if (currentFile) acceptFile(currentFile);
}

async function poll(jobId) {
  clearTimeout(pollTimer);
  const r = await fetch(`api/status/${jobId}`, { credentials: 'same-origin' });
  if (r.status === 401) { location.reload(); return; }
  const d = await r.json().catch(() => null);
  if (!d) { setError('读取任务状态失败。'); return; }

  if (d.status === 'queued' || d.status === 'running') {
    const label = d.status === 'queued' ? '排队中…' : '正在转换…';
    statusBox.innerHTML = `<span class="st-running">${label}</span>` +
      `<div class="muted" style="margin-top:6px;font-size:13px">${d.orig} → .${d.target}（${fmtSize(d.size_in)}）</div>`;
    upsertDone({ job: jobId, name: d.orig, from: extOf(d.orig), to: d.target,
                 status: d.status, size: 0 });
    pollTimer = setTimeout(() => poll(jobId), 1200);
    return;
  }

  if (d.status === 'done') {
    const extra = d.multi ? `（共 ${d.multi} 个文件，已打包成 zip）` : '';
    statusBox.innerHTML =
      `<span class="st-done">转换完成</span>${extra}` +
      `<div class="muted" style="margin-top:6px;font-size:13px">${d.orig} → .${d.target} · 产出 ${fmtSize(d.size)}</div>`;
    const a = document.createElement('a');
    a.className = 'dl';
    a.href = d.download;
    a.textContent = '下载结果';
    statusBox.append(document.createElement('br'), a);
    upsertDone({ job: jobId, name: d.orig, from: extOf(d.orig), to: d.target,
                 status: 'done', size: d.size });
    resetButtons();
    return;
  }

  setError(d.error || '转换失败。');
  statusBox.innerHTML = '';
  upsertDone({ job: jobId, name: d.orig || '', from: extOf(d.orig || ''), to: d.target || '',
               status: 'failed', size: 0 });
  resetButtons();
}

/* ---------------- 本次已处理的文件（只存在浏览器里，服务器不保存列表）----------------
   为什么这么改：全家共用一个口令，服务端分不清谁是谁。原先服务端存一份任务列表
   并提供 /api/history 列举接口，实测结果是「任何登录的人都能看到别人的文件名、
   还能下载别人的文件」。
   现在服务端**没有任何列举接口**——列表由浏览器自己记，任务号（122 位随机 UUID）
   只有创建者手里有，别人无从得知，也就不可能看到或拿到你的文件。
   存 sessionStorage：刷新页面仍在，关掉标签页即清空 —— 正好对应「这次」。*/

const DONE_KEY = 'fc_done';
const doneBody = $('done');

function extOf(name) {
  const i = (name || '').lastIndexOf('.');
  return i > 0 ? name.slice(i + 1).toLowerCase() : '';
}
function loadDone() {
  try { return JSON.parse(sessionStorage.getItem(DONE_KEY) || '[]'); } catch (_) { return []; }
}
function saveDone(list) {
  try { sessionStorage.setItem(DONE_KEY, JSON.stringify(list.slice(0, 100))); } catch (_) { /* 配额满则忽略 */ }
}
function upsertDone(item) {
  const list = loadDone().filter((x) => x.job !== item.job);
  list.unshift(item);
  saveDone(list);
  renderDone();
}
function dropDone(jobId) {
  saveDone(loadDone().filter((x) => x.job !== jobId));
  renderDone();
}

const ST_LABEL = { queued: '排队中', running: '转换中', done: '完成', failed: '失败' };
const ST_CLS = { done: 'st-done', failed: 'st-failed', running: 'st-running', queued: 'st-running' };

function renderDone() {
  const list = loadDone();
  doneBody.innerHTML = '';
  if (!list.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    td.className = 'muted';
    td.textContent = '本次还没有处理过文件。';
    tr.append(td);
    doneBody.append(tr);
    $('clear-done').hidden = true;
    return;
  }
  for (const it of list) {
    const tr = document.createElement('tr');
    const cell = (txt, c) => {
      const e = document.createElement('td');
      if (c) e.className = c;
      e.textContent = txt;
      return e;
    };
    tr.append(cell(it.name || '—'),
              cell(it.from ? '.' + it.from : '—'),
              cell(it.to ? '.' + it.to : '—'),
              cell(ST_LABEL[it.status] || it.status, ST_CLS[it.status]),
              cell(it.size ? fmtSize(it.size) : '—'));
    const acts = document.createElement('td');
    acts.className = 'acts';
    if (it.status === 'done') {
      const a = document.createElement('a');
      a.href = `download/${it.job}`;
      a.textContent = '下载';
      acts.append(a);
    }
    if (it.status !== 'running') {
      const del = document.createElement('a');
      del.href = '#';
      del.className = 'del';
      del.textContent = '删除';
      del.addEventListener('click', (e) => { e.preventDefault(); deleteDone(it.job, del); });
      acts.append(del);
    }
    tr.append(acts);
    doneBody.append(tr);
  }
  $('clear-done').hidden = false;
}

/* 删除一条：服务器上的输入与产出也要一起删（不是只从列表里去掉）。 */
async function deleteDone(jobId, el) {
  if (!confirm('确定删除这条记录？\n上传的文件和转换结果会一起从服务器删除，无法恢复。')) return;
  const old = el.textContent;
  el.textContent = '删除中…';
  try {
    const r = await fetch(`api/delete/${jobId}`, { method: 'POST', credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok && r.status !== 404) {     // 404 = 服务器上已被清理，本地照样移除
      const d = await r.json().catch(() => ({}));
      setError(d.detail || '删除失败，请重试。');
      el.textContent = old;
      return;
    }
    dropDone(jobId);
  } catch (_) {
    setError('删除失败：连不上服务器。');
    el.textContent = old;
  }
}

/* 清空本次记录：只提交"我自己列表里"的任务号，服务器不接受任何"全清"指令。 */
$('clear-done').addEventListener('click', async () => {
  const list = loadDone();
  if (!list.length) return;
  if (!confirm('确定清空本次记录？\n已上传的文件和转换结果会一起从服务器删除，无法恢复。')) return;
  const btn = $('clear-done');
  btn.disabled = true;
  btn.textContent = '清理中…';
  try {
    const r = await fetch('api/delete-batch', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jobs: list.map((x) => x.job) }),
    });
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) setError('清空失败，请重试。');
    saveDone([]);
    renderDone();
  } catch (_) {
    setError('清空失败：连不上服务器。');
  }
  btn.disabled = false;
  btn.textContent = '清空本次记录';
});

$('logout').addEventListener('click', async () => {
  await fetch('api/logout', { method: 'POST', credentials: 'same-origin' });
  // 顺手清掉记录列表：退出后可能换个人在同一个浏览器上登录，别让他看到上一个人的记录
  try { sessionStorage.removeItem(DONE_KEY); } catch (_) { /* 忽略 */ }
  location.reload();
});

/* 离开页面时把服务器上的文件也一并删掉 —— 不只是界面上的记录。
   为什么用 sendBeacon：页面卸载过程中普通 fetch 会被浏览器中断，只有 sendBeacon
   是"交给浏览器排队发送"，才能真正送达。
   为什么用 pagehide 而不是 beforeunload：手机上（尤其 iOS）beforeunload 基本不触发。
   e.persisted 为真表示是进 bfcache（切后台/按后退又回来），这种情况不能清。 */
function purgeOnLeave() {
  const jobs = loadDone().map((x) => x.job).filter(Boolean);
  if (!jobs.length) return;
  const body = JSON.stringify({ jobs });
  // 本地列表一起清掉：服务器上的文件都没了，留着这几行只会点下载报错
  try { sessionStorage.removeItem(DONE_KEY); } catch (_) { /* 忽略 */ }
  try {
    if (navigator.sendBeacon &&
        navigator.sendBeacon('api/delete-batch', new Blob([body], { type: 'application/json' }))) {
      return;
    }
  } catch (_) { /* 落到下面的兜底方式 */ }
  try {
    fetch('api/delete-batch', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json' }, body,
    });
  } catch (_) { /* 页面正在卸载，失败也无所谓：2 小时 TTL 会兜底清理 */ }
}

window.addEventListener('pagehide', (e) => {
  if (e.persisted) return;          // 进 bfcache，不算离开
  purgeOnLeave();
});

renderDone();
