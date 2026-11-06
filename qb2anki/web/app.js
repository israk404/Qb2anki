/* QB to Anki: the whole app screen. Plain JavaScript, no libraries. */
(() => {
'use strict';
const TOKEN = window.QB_TOKEN;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k.startsWith('data-') || k === 'for') e.setAttribute(k, v);
    else if (k in e) { try { e[k] = v; } catch (_) { e.setAttribute(k, v); } }
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

async function api(path, body, method) {
  const opt = { headers: { 'X-QB-Token': TOKEN } };
  if (body !== undefined) { opt.method = method || 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) { const err = new Error(data.error || ('Request failed (' + r.status + ')')); err.status = r.status; throw err; }
  return data;
}

function toast(msg, kind) {
  const t = h('div', { class: 'toast' + (kind === 'err' ? ' err' : ''), text: msg });
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), 'err');
const fmtMin = (s) => (s >= 60 ? Math.floor(s / 60) + ' min ' + (s % 60) + ' s' : s + ' s');

/* ------------------------------------------------------------------ state, navigation */
const S = { status: null, view: 'home', jobId: null, since: 0, lines: [], poll: null, hidden: false, lastJob: null, suggest: null };
const VIEWS = [['home', 'Home'], ['read', 'Read photos'], ['review', 'Review'], ['export', 'Export to Anki'],
               ['settings', 'Settings'], ['database', 'Database'], ['help', 'Help']];

function renderNav() {
  const nav = $('#nav');
  nav.textContent = '';
  nav.append(h('div', { class: 'brand' }, 'QB to Anki', h('small', { text: 'question banks to flashcards' })));
  for (const [id, label] of VIEWS) {
    const n = id === 'review' && S.status ? S.status.counts.review : 0;
    nav.append(h('button', { class: 'item' + (S.view === id ? ' on' : ''), 'data-view': id, onclick: () => go(id) },
      h('span', { text: label }), n ? h('span', { class: 'badge', text: n }) : null));
  }
  nav.append(h('div', { class: 'spacer' }),
    h('button', { class: 'btn small quit', onclick: quit, text: 'Quit app' }));
}

async function quit() {
  if (!confirm('Close the app? Anything running will stop.')) return;
  try { await api('/api/quit', {}); } catch (_) { /* server is going away */ }
  document.body.textContent = 'You can close this tab now.';
}

async function refreshStatus() {
  try { S.status = await api('/api/status'); renderNav(); } catch (e) { /* server busy or closed */ }
  return S.status;
}

function go(view) {
  S.view = view;
  location.hash = view;
  renderNav();
  const main = $('#main');
  main.textContent = '';
  const fn = {home: viewHome, read: viewRead, review: viewReview, export: viewExport, settings: viewSettings,
              database: viewDatabase, help: viewHelp}[view];
  fn(main).catch(fail);
}

/* ------------------------------------------------------------------ background jobs (the activity drawer) */
function renderActivity(j) {
  const box = $('#activity');
  box.hidden = false;
  box.textContent = '';
  const running = j.status === 'running';
  const label = {running: 'Working…', done: 'Finished', stopped: 'Stopped', error: 'Problem'}[j.status] || j.status;
  box.append(h('div', { class: 'bar' },
    h('span', { class: 'dot ' + j.status }), h('span', { class: 'grow', text: j.name + ' · ' + label + ' · ' + fmtMin(j.seconds) }),
    running ? h('button', { class: 'btn small danger', onclick: () => api('/api/job/stop', {}).catch(fail), text: 'Stop' }) : null,
    h('button', { class: 'btn small', onclick: () => { S.hidden = !S.hidden; renderActivity(j); }, text: S.hidden ? 'Show log' : 'Hide log' }),
    !running ? h('button', { class: 'btn small', onclick: () => { box.hidden = true; }, text: 'Close' }) : null));
  if (!S.hidden) {
    const pre = h('pre', { class: 'mono', id: 'log', text: S.lines.join('\n') || 'Starting…' });
    box.append(pre);
    pre.scrollTop = pre.scrollHeight;
  }
}

async function pollJob() {
  clearTimeout(S.poll);
  let j;
  try {
    j = await api('/api/job?since=' + (S.jobId ? S.since : 0));
    if (j.status === 'none') return;
    if (S.jobId !== j.id) { S.jobId = j.id; S.since = 0; S.lines = []; j = await api('/api/job?since=0'); }
  } catch (e) { S.poll = setTimeout(pollJob, 2000); return; }
  S.lines.push(...j.lines);
  S.since = j.next;
  renderActivity(j);
  if (j.status === 'running') { S.poll = setTimeout(pollJob, 1000); return; }
  S.lastJob = j;
  if (j.result && j.result.suggest) S.suggest = j.result.suggest;
  toast(j.name + ': ' + ({done: 'finished', stopped: 'stopped', error: 'stopped with a problem'}[j.status] || j.status), j.status === 'error' ? 'err' : undefined);
  await refreshStatus();
  // refresh pages that show the task's results, but never a page where you may be typing
  if (['home', 'read', 'database', 'export'].includes(S.view) || (S.view === 'settings' && /working models|Testing keys/.test(j.name))) go(S.view);
}

async function startJob(path, body) {
  try {
    await api(path, body || {});
    S.jobId = null; S.since = 0; S.lines = []; S.hidden = false;
    pollJob();
    refreshStatus();
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ small building blocks */
const chip = (text, kind) => h('span', { class: 'chip' + (kind ? ' ' + kind : ''), text });
const stat = (n, label) => h('div', { class: 'card stat' }, h('b', { text: n }), h('span', { text: label }));
const head = (title, sub) => h('div', { class: 'page-head' }, h('h1', { text: title }), sub ? h('p', { class: 'mute', text: sub }) : null);
const field = (label, input, hint) => h('div', {}, h('label', { class: 'f', text: label }), input, hint ? h('div', { class: 'mute small', text: hint }) : null);
const check = (id, label, hint, on) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', id, checked: !!on }), h('span', {}, label, hint ? h('small', { text: hint }) : null));

/* ------------------------------------------------------------------ Home */
async function viewHome(el) {
  const st = await refreshStatus();
  const c = st.counts;
  let next;
  if (!st.has_key) next = ['Add your Google AI key', 'The app needs a key to read photos.', 'Open Settings', () => go('settings')];
  else if (st.inbox.waiting > 0) next = [`Read ${st.inbox.waiting} waiting photo${st.inbox.waiting > 1 ? 's' : ''}`, 'New photos are in the inbox.', 'Read photos', () => go('read')];
  else if (c.review > 0) next = [`${c.review} card${c.review > 1 ? 's' : ''} need your review`, 'Check them against the photo, then approve.', 'Start reviewing', () => go('review')];
  else if (st.exportable > 0) next = [`${st.exportable} card${st.exportable > 1 ? 's are' : ' is'} ready for Anki`, 'Create the file and import it.', 'Export', () => go('export')];
  else next = ['All caught up', 'Add photos on the Read photos page to continue.', 'Add photos', () => go('read')];
  el.append(head('Welcome back', 'Photos in, flashcards out. Here is where things stand.'));
  el.append(h('div', { class: 'card next' }, h('div', {}, h('h2', { text: next[0] }), h('div', { class: 'mute', text: next[1] })),
    h('button', { class: 'btn primary', onclick: next[3], text: next[2] })));
  el.append(h('div', { class: 'grid g4', style: 'margin-top:16px' },
    stat(st.inbox.waiting, 'photos waiting'), stat(c.review, 'cards need review'), stat(st.exportable, 'ready to export'), stat(st.stats.exported, 'cards exported so far')));
  const on = (x) => (x ? chip('on', 'good') : chip('off', 'grey'));
  el.append(h('div', { class: 'grid g2', style: 'margin-top:16px' },
    h('div', { class: 'card' }, h('h2', { text: 'Requests today' }),
      st.usage.length ? h('table', {}, h('tr', {}, h('th', { text: 'model' }), h('th', { text: 'requests' }), h('th', { text: 'tokens in / out' })),
        st.usage.map((u) => h('tr', {}, h('td', { text: u.model }), h('td', { text: u.calls }), h('td', { text: u.input_tokens.toLocaleString() + ' / ' + u.output_tokens.toLocaleString() }))))
        : h('p', { class: 'mute', text: 'Nothing requested yet today.' }),
      h('p', { class: 'mute small', text: 'Free daily limits reset at ' + st.reset_hint + '. Your real limits: ai.dev/rate-limit' })),
    h('div', { class: 'card' }, h('h2', { text: 'How it reads' }),
      h('table', {}, h('tr', {}, h('td', { text: 'AI proofreads every page against the photo' }), h('td', {}, on(st.mode.proofread))),
        h('tr', {}, h('td', { text: 'AI answers and explanations where the source has none' }), h('td', {}, on(st.mode.ai_fill))),
        h('tr', {}, h('td', { text: 'Math check and repair' }), h('td', {}, on(st.mode.math_check))),
        h('tr', {}, h('td', { text: 'Read every page twice (cross-check)' }), h('td', {}, on(st.mode.cross_check))),
        h('tr', {}, h('td', { text: 'Automatic backup before each task' }), h('td', {}, on(st.mode.auto_backup))),
        h('tr', {}, h('td', { text: 'Key rotation' }), h('td', {}, st.rotation ? chip(st.rotation.keys + ' keys', 'soft') : chip('off', 'grey')))),
      h('p', { class: 'mute small', text: 'Change these in Settings.' }))));
}

/* ------------------------------------------------------------------ Read photos */
const PROFILE_HELP = { auto: 'Detect automatically (recommended)', book: 'Photos of the book’s two-page spreads', screenshot: 'Screenshots, PDF pages, web quizzes: one page per image' };
const store = { get(k, d) { try { const v = localStorage.getItem('qb.' + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
                set(k, v) { try { localStorage.setItem('qb.' + k, JSON.stringify(v)); } catch (_) { /* private window */ } } };

async function viewRead(el) {
  const st = await refreshStatus();
  const inbox = await api('/api/inbox');
  el.append(head('Read photos', 'Add photos, then let the app read them. Finished work is saved even if you stop.'));

  const profiles = Object.keys(st.profiles).sort((a, b) => (a === 'auto' ? -1 : b === 'auto' ? 1 : 0));
  const sel = h('select', { id: 'upProfile' }, profiles.map((p) => h('option', { value: p, selected: p === st.default_profile,
    text: p === 'auto' ? PROFILE_HELP.auto : p + ' — ' + (PROFILE_HELP[p] || '') })));
  const autoStart = h('input', { type: 'checkbox', id: 'autoStart', checked: store.get('autoStart', true), onchange: () => store.set('autoStart', autoStart.checked) });
  const exam = h('input', { type: 'text', id: 'upExam', placeholder: 'Exam name (optional, used when the image has no title)' });
  const fileIn = h('input', { type: 'file', multiple: true, accept: 'image/*', hidden: true });
  const results = h('div', { class: 'small mute', style: 'margin-top:8px' });
  const drop = h('div', { class: 'drop', onclick: () => fileIn.click() }, 'Drop photos here or click to choose');
  async function send(files) {
    let ok = 0;
    const bad = [];
    results.textContent = 'Adding ' + files.length + ' file(s)…';
    for (const f of files) {
      try {
        const r = await fetch('/api/upload', { method: 'POST', body: f, headers: { 'X-QB-Token': TOKEN, 'X-Filename': encodeURIComponent(f.name),
          'X-Profile': encodeURIComponent(sel.value), 'X-Exam': encodeURIComponent(exam.value) } });
        const j = await r.json();
        if (r.ok) ok += 1; else bad.push(f.name + ': ' + j.error);
      } catch (e) { bad.push(f.name + ': ' + e.message); }
    }
    if (ok) toast(`Added ${ok} photo${ok > 1 ? 's' : ''} to the inbox.`);
    bad.forEach((b) => toast(b, 'err'));
    await refreshStatus();
    if (ok && autoStart.checked && !S.status.busy) { await startJob('/api/run', {}); }
    go('read');
  }
  fileIn.addEventListener('change', () => send([...fileIn.files]));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); send([...e.dataTransfer.files]); });
  el.append(h('div', { class: 'card' }, h('h2', { text: '1. Add photos' }),
    h('div', { class: 'grid g2' }, field('What are they?', sel), field('Exam name', exam)),
    h('label', { class: 'chk' }, autoStart, h('span', {}, 'Start reading right after adding')),
    h('div', { style: 'margin-top:4px' }, drop, fileIn), results,
    h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn small', onclick: () => api('/api/open', { what: 'inbox' }).catch(fail), text: 'Open inbox folder' }),
      h('span', { class: 'mute small', text: 'You can also copy files straight into the inbox folder.' }))));

  const waiting = inbox.filter((f) => f.status !== 'done').length;
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: `2. Inbox (${waiting} waiting, ${inbox.length} total)` }),
    inbox.length ? h('table', {}, h('tr', {}, ['file', 'what', 'exam', 'status', 'size'].map((t) => h('th', { text: t }))),
      inbox.map((f) => h('tr', {}, h('td', { text: f.path }), h('td', { text: f.profile }), h('td', { text: f.exam || '—' }),
        h('td', {}, f.status === 'done' ? chip('read', 'good') : f.status === 'failed' ? h('span', { title: f.error }, chip('failed', 'hard'), ' ', h('span', { class: 'mute small', text: f.error.slice(0, 80) })) : chip('waiting', 'soft')),
        h('td', { text: f.kb + ' KB' }))))
      : h('p', { class: 'mute', text: 'The inbox is empty.' })));

  const limit = h('input', { type: 'number', min: 1, id: 'runLimit', placeholder: 'all' });
  const failed = inbox.filter((f) => f.status === 'failed').length;
  const rot = h('select', { id: 'runType' }, [['', 'as chosen when added'], ['auto', 'detect automatically for all']]
    .map(([v, t]) => h('option', { value: v, text: t, selected: failed > 0 && v === 'auto' })));
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: '3. Read' }),
    failed ? h('div', { class: 'notice', text: failed + ' photo(s) failed. They are tried again when you press Read; “detect automatically for all” usually fixes a wrong photo type or a sideways photo.' }) : null,
    h('div', { class: 'grid g3' }, field('How many photos now?', limit, 'Leave empty for all waiting photos.'), field('Photo type for this run', rot, 'Use “detect” to retry failed photos.'), ''),
    h('div', { class: 'row', style: 'margin-top:14px' },
      h('button', { class: 'btn primary', disabled: S.status.busy || !waiting, onclick: () => startJob('/api/run', { limit: limit.value, profile: rot.value }), text: 'Read waiting photos' }),
      h('span', { class: 'mute small', text: S.status.busy ? 'A task is already running.' : (waiting ? 'Progress appears at the bottom of the screen.' : 'Nothing waiting.') }))));
}

/* ------------------------------------------------------------------ Review */
const R = { flt: 'review', items: [], cur: null };
const LABELS = ['ক', 'খ', 'গ', 'ঘ', 'ঙ'];

async function viewReview(el) {
  await refreshStatus();
  const c = S.status.counts;
  el.append(head('Review', 'Compare each card with its photo, fix anything wrong, then approve it.'));
  const sel = h('select', { id: 'flt', onchange: () => { R.flt = sel.value; R.cur = null; loadList().catch(fail); } },
    [['review', `Needs review (${c.review})`], ['soft', `Exportable, with notes (${c.soft})`], ['ai', `Answer found by AI (${c.ai})`],
     ['all', `All cards (${c.all})`], ['approved', 'Approved'], ['rejected', 'Rejected']].map(([v, t]) => h('option', { value: v, selected: v === R.flt, text: t })));
  el.append(h('div', { class: 'review' }, h('div', { class: 'card rlist' }, h('div', { class: 'head' }, sel), h('div', { class: 'items', id: 'ritems' })), h('div', { id: 'rdetail' })));
  await loadList();
}

async function loadList(keep) {
  R.items = await api('/api/queue?f=' + R.flt);
  const L = $('#ritems');
  if (!L) return;
  L.textContent = '';
  for (const it of R.items) {
    const meta = h('span', {});
    if (it.hard) meta.append(chip(it.hard + ' check', 'hard'), ' ');
    if (it.soft) meta.append(chip(it.soft + ' note', 'soft'));
    L.append(h('div', { class: 'item' + (R.cur === it.id ? ' sel' : ''), 'data-id': it.id, onclick: () => openQ(it.id).catch(fail) },
      h('div', { class: 't' }, h('span', { text: 'p.' + (it.page ?? '?') + ' · Q' + (it.number ?? '?') + ' · ' + it.subject }), meta),
      h('div', { class: 's', text: it.snippet || '(empty)' })));
  }
  if (!keep) {
    if (R.items.length) await openQ(R.items[0].id);
    else $('#rdetail').replaceChildren(h('div', { class: 'empty card' }, R.flt === 'review' ? 'Nothing needs review. 🎉' : 'Nothing here.'));
  }
}

async function openQ(id) {
  R.cur = id;
  const q = await api('/api/question/' + id);
  $$('#ritems .item').forEach((x) => x.classList.toggle('sel', x.dataset.id == id));
  renderDetail(q);
}

function renderDetail(q) {
  const box = $('#rdetail');
  if (!box) return;
  box.textContent = '';
  const subj = h('select', { id: 'subject' }, q.subjects.map((s) => h('option', { value: s, selected: s === q.subject, text: s })));
  const ex = h('select', { id: 'exam' }, h('option', { value: '', selected: q.exam_id === null, text: '(exam not known yet)' }),
    q.exams.map((e) => h('option', { value: e.id, selected: e.id === q.exam_id, text: e.name })));
  const unknown = q.exam_id === null;
  const nameBtn = h('button', { class: 'btn small', text: unknown ? 'Name this exam…' : 'Rename exam', onclick: async () => {
    const n = prompt('Short exam name (used for tags and the source line):', unknown ? '' : q.exam_name);
    if (n === null) return;
    try { await api(unknown ? '/api/exam/new' : '/api/exam/' + q.exam_id, unknown ? { question_id: q.id, label: n } : { label: n }); await openQ(q.id); } catch (e) { fail(e); }
  } });
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, h('div', { class: 'mute', style: 'flex:1', text: 'Page ' + (q.printed_page ?? '?') + ' · Question ' + (q.number ?? '?') + ' · ' + q.status
    + (q.answer_source === 'ai' ? ' · answer by AI' : '') }), subj, ex, nameBtn));

  if ((q.flags || []).length) {
    box.append(h('ul', { class: 'flags' }, q.flags.map((f) => h('li', { class: f.hard ? 'hard' : 'soft', text: (f.hard ? 'Must check: ' : 'Note: ') + f.msg }))));
  }

  // photo card with tabs
  const pane = h('div');
  const tabsEl = h('div', { class: 'tabs' });
  const kinds = [['Question', q.crop_url], ['Page', q.context_url], ['Layout', q.overlay_url], ['Figure', q.figure_url], ['Card look', q.correct_label ? 'look' : null]].filter((k) => k[1]);
  function show(i) {
    $$('button', tabsEl).forEach((b, j) => b.classList.toggle('on', i === j));
    pane.textContent = '';
    const [name, url] = kinds[i];
    if (url === 'look') return showLook(pane, q);
    const img = h('img', { src: url, alt: name, onclick: () => img.classList.toggle('big') });
    pane.append(img);
  }
  kinds.forEach(([name], i) => tabsEl.append(h('button', { text: name, onclick: () => show(i) })));
  const shot = h('div', { class: 'card shot' }, h('h2', { text: 'The photo (click a picture to enlarge)' }), tabsEl, pane);
  if (kinds.length) show(0);

  // form card
  const form = h('div', { class: 'card' }, h('h2', { text: 'The card' }));
  form.append(h('label', { class: 'f', text: 'Question' }), h('textarea', { id: 'stem', value: q.stem || '' }));
  if (q.alt && q.alt.stem && q.alt.stem !== q.stem) form.append(h('div', { class: 'alt', text: 'Second reading: ' + q.alt.stem }));
  form.append(h('label', { class: 'f', text: 'Options (select the correct answer)' }));
  const opts = q.options && q.options.length ? q.options : LABELS.slice(0, 4).map((l) => ({ label: l, shown: l, text: '' }));
  opts.forEach((o, i) => {
    const alt = q.alt && q.alt.options && q.alt.options[i];
    form.append(h('div', { class: 'opt' }, h('label', {}, h('input', { type: 'radio', name: 'ans', value: o.label, checked: q.correct_label === o.label }), (o.shown || o.label) + ')'),
      h('input', { type: 'text', class: 'otext', value: o.text, 'data-label': o.label, 'data-shown': o.shown || o.label })));
    if (alt && alt.text !== o.text) form.append(h('div', { class: 'alt', text: 'Second reading: ' + alt.text }));
  });
  form.append(h('label', { class: 'f', text: 'Explanation (from the source)' }), h('textarea', { id: 'expl', value: q.explanation || '', rows: 7 }));
  if (q.proof) {
    const pf = q.proof;
    const lines = [];
    if (pf.stem !== undefined && pf.stem !== q.stem) lines.push('Question: ' + pf.stem);
    (pf.options || []).forEach((o, i) => { const now = q.options[i]; if (now && o.text !== now.text) lines.push('Option ' + (o.shown || o.label) + ': ' + o.text); });
    if (pf.explanation !== undefined && pf.explanation !== (q.explanation_original ? q.explanation_original : q.explanation)) lines.push('Explanation: ' + pf.explanation);
    form.append(h('div', { class: 'alt' }, 'First reading, before AI proofreading' + (pf.note ? ' (' + pf.note + ')' : '') + ':', h('br'),
      lines.length ? lines.join('\n') : '(the same as now)', h('br'),
      h('button', { class: 'btn small', text: 'Restore the first reading', onclick: () => {
        if (pf.stem !== undefined) $('#stem').value = pf.stem;
        $$('.otext').forEach((inp, i) => { const o = (pf.options || [])[i]; if (o) inp.value = o.text; });
        if (pf.explanation !== undefined) $('#expl').value = pf.explanation;
        toast('First reading restored in the form. Press Save or Approve to keep it.');
      } })));
  }
  if (q.explanation_original) {
    form.append(h('div', { class: 'alt' }, 'First reading, before the AI repaired it: ', h('br'), q.explanation_original, h('br'),
      h('button', { class: 'btn small', onclick: () => { $('#expl').value = q.explanation_original; }, text: 'Use this version' })));
  }
  if (q.alt && q.alt.explanation && q.alt.explanation !== q.explanation) form.append(h('div', { class: 'alt', text: 'Second reading: ' + q.alt.explanation }));
  if (!(q.explanation || '').trim() || q.ai_explanation) {
    form.append(h('label', { class: 'f', text: 'AI-written explanation (shown on the card only when the source has none; clear it to remove)' }),
      h('textarea', { id: 'aiexpl', value: q.ai_explanation || '', rows: 4 }));
  }
  if (q.appearances.length > 1) form.append(h('div', { class: 'mute small', style: 'margin-top:8px', text: 'Also appears: ' + q.appearances.slice(1).map((a) => a.exam + ' (Q' + a.number + ', p.' + a.page + ')').join('; ') }));
  box.append(h('div', { class: 'grid g2' }, shot, form));

  const msg = h('div', { class: 'msg', id: 'rmsg' });
  box.append(h('div', { class: 'actions' }, msg,
    h('button', { class: 'btn danger', text: q.status === 'rejected' ? 'Move back to review' : 'Reject', onclick: () => act(q.status === 'rejected' ? 'reopen' : 'reject', true) }),
    h('button', { class: 'btn', text: 'Skip', onclick: () => { const i = R.items.findIndex((x) => x.id === R.cur); const n = R.items[i + 1]; if (n) openQ(n.id).catch(fail); } }),
    h('button', { class: 'btn', text: 'Save', onclick: () => act('save', false) }),
    h('button', { class: 'btn primary', text: 'Approve & next (Ctrl+Enter)', onclick: () => act('approve', true) })));
}

async function showLook(pane, q) {
  pane.append(h('div', { class: 'mute small', text: 'Loading…' }));
  let d;
  try { d = await api('/api/preview/' + q.id); } catch (e) { pane.textContent = e.message; return; }
  let night = false;
  const frame = h('iframe', { title: 'card preview' });
  const draw = () => {
    frame.srcdoc = '<!doctype html><meta charset="utf-8"><body class="card ' + (night ? 'nightMode' : '') + '" style="margin:0;padding:16px;font:20px arial;background:'
      + (night ? '#2f2f31;color:#fff' : '#fff;color:#000') + '">' + d.front + '<hr id="answer">' + d.back + '</body>';
  };
  draw();
  pane.textContent = '';
  pane.append(h('div', { class: 'row', style: 'margin-bottom:6px' }, h('button', { class: 'btn small', text: 'Day / night', onclick: () => { night = !night; draw(); } }),
    h('span', { class: 'mute small', text: 'As it will look in Anki (formulas are drawn by Anki). Shows the saved card.' })), frame);
}

function collect(action) {
  const options = $$('.otext').map((i) => ({ label: i.dataset.label, shown: i.dataset.shown, text: i.value }));
  const sel = $('input[name=ans]:checked');
  return { action, stem: $('#stem').value, options, correct_label: sel ? sel.value : null, explanation: $('#expl').value,
    ai_explanation: $('#aiexpl') ? $('#aiexpl').value : '', subject: $('#subject').value, exam_id: $('#exam').value };
}

async function act(action, advance) {
  if (R.cur === null) return;
  const msg = $('#rmsg');
  try { await api('/api/question/' + R.cur, collect(action)); } catch (e) { if (msg) { msg.textContent = e.message; msg.className = 'msg err'; } return; }
  const done = { approve: 'Approved', reject: 'Rejected', reopen: 'Moved back to review', save: 'Saved' }[action];
  const idx = R.items.findIndex((i) => i.id === R.cur);
  await loadList(true);
  refreshStatus();
  const say = (t) => { const m = $('#rmsg'); if (m) m.textContent = t; };
  if (!advance) { await openQ(R.cur); say(done); return; }
  // In the review queue the finished card disappears, so the card that slid into its place is next;
  // in "All cards" it stays in the list, so next is the one after it.
  const pos = R.items.findIndex((i) => i.id === R.cur);
  const next = pos >= 0 ? R.items[pos + 1] : R.items[Math.min(idx, R.items.length - 1)];
  if (next) { await openQ(next.id); say(done); }
  else if (pos >= 0) { await openQ(R.cur); say(done + '. That was the last card in this list.'); }
  else { R.cur = null; $('#rdetail').replaceChildren(h('div', { class: 'empty card' }, 'Nothing left in this list. 🎉')); }
}

document.addEventListener('keydown', (e) => {
  if (S.view === 'review' && e.ctrlKey && e.key === 'Enter' && $('#stem')) { e.preventDefault(); act('approve', true); }
});

/* ------------------------------------------------------------------ Export */
async function viewExport(el) {
  const info = await api('/api/export/info');
  el.append(head('Export to Anki', 'Create one file with your approved cards, then import it into Anki.'));
  el.append(h('div', { class: 'grid g3' }, stat(info.ready, 'cards ready to export'), stat(info.held, 'held back (still need review)'), stat(info.exported, 'exported before')));
  const all = h('input', { type: 'checkbox', id: 'expAll' });
  const media = h('select', { id: 'expMedia' }, h('option', { value: '', text: 'No — I will copy the images myself' }), info.media_dirs.map((d) => h('option', { value: d, text: d })));
  const custom = h('input', { type: 'text', id: 'expMediaCustom', placeholder: 'or paste the path of Anki’s collection.media folder' });
  const result = h('div', { id: 'expResult' });
  const go_ = h('button', { class: 'btn primary', text: 'Create the Anki file', onclick: async () => {
    try {
      const r = await api('/api/export', { all: all.checked, anki_media: custom.value || media.value });
      result.textContent = '';
      if (!r.count) { result.append(h('div', { class: 'notice', text: 'Nothing to export: no approved or clean cards that were not already exported.' })); return; }
      result.append(h('div', { class: 'notice good' }, `Created ${r.count} cards: `, h('a', { href: '/download/' + r.file, text: r.file }), '. Now import it into Anki (steps below).'),
        r.skipped.length ? h('div', { class: 'notice', text: r.skipped.length + ' card(s) were skipped: ' + r.skipped.map((s) => '#' + s.id + ' ' + s.why).join('; ') }) : null);
      refreshStatus();
    } catch (e) { fail(e); }
  } });
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Create the file' }),
    h('label', { class: 'chk' }, all, h('span', {}, 'Include cards exported before', h('small', { text: 'Use after deleting the old cards in Anki, to import everything again.' }))),
    field('Copy pictures (figures) into Anki’s media folder automatically?', media), custom,
    h('div', { class: 'row', style: 'margin-top:14px' }, go_, h('button', { class: 'btn', text: 'Open the output folder', onclick: () => api('/api/open', { what: 'out' }).catch(fail) })), result));
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Import into Anki' }),
    h('ol', { class: 'steps' },
      h('li', { text: 'In Anki choose File → Import, and pick the newest anki_….txt file from the output folder (once only).' }),
      h('li', { text: 'Leave “Existing notes” on Update. The cards go to the decks “' + info.deck_root + '::<subject>”, using the note type “' + info.notetype + '”.' }),
      h('li', { text: 'If Anki says “notetype was missing”, create a note type named exactly “' + info.notetype + '” (Tools → Manage Note Types → Add → Add: Basic), or change the name in Settings → Anki.' }),
      h('li', { text: 'Pictures: unless you chose a media folder above, copy the files from out\\media into Anki’s collection.media folder.' }),
      h('li', { text: 'Importing the same file again only reports “skipped”: the cards are already there.' }))));
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Earlier files' }),
    info.files.length ? h('ul', { class: 'steps' }, info.files.map((f) => h('li', {}, h('a', { href: '/download/' + f, text: f })))) : h('p', { class: 'mute', text: 'None yet.' })));
}

/* ------------------------------------------------------------------ Settings */
async function viewSettings(el) {
  const [cfg, keys, st] = await Promise.all([api('/api/settings'), api('/api/keys'), refreshStatus()]);
  const save = async (patch, ok) => { try { await api('/api/settings', patch); toast(ok || 'Saved. It applies from the next task.'); } catch (e) { fail(e); } };
  el.append(head('Settings', 'Set it up once. Changes apply from the next task you start.'));

  // --- API keys
  const mainKey = h('input', { type: 'password', id: 'mainKey', placeholder: keys.main_hint ? 'A key is saved (' + keys.main_hint + '). Paste a new one to replace it.' : 'Paste your Google AI (Gemini) API key', autocomplete: 'off' });
  const keyList = h('textarea', { id: 'keyList', rows: 6, placeholder: 'Optional: more keys, one per line (see the warning)', value: keys.text });
  const rot = keys.rotation;
  el.append(h('div', { class: 'card' }, h('h2', { text: 'Google AI key' }),
    h('p', { class: 'mute', text: 'Create a free key at aistudio.google.com/apikey. It stays on this computer.' }),
    field('Your key', mainKey),
    h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn primary', text: 'Save key', onclick: async () => {
      try { const r = await api('/api/apikey', { key: mainKey.value }); mainKey.value = ''; toast('Key saved (' + r.hint + ').'); refreshStatus(); } catch (e) { fail(e); } } }),
      h('button', { class: 'btn', text: 'Check setup', onclick: () => startJob('/api/doctor') })),
    h('h3', { style: 'margin-top:22px', text: 'Key rotation (optional, at your own risk)' }),
    h('div', { class: 'notice', text: 'Google’s terms do not allow using several projects to get around a quota, and projects have been restricted for it. Only use keys and accounts you are willing to lose. The app stops by itself if Google blocks two keys.' }),
    keyList,
    h('div', { class: 'row', style: 'margin-top:8px' },
      h('button', { class: 'btn', text: 'Save key list', onclick: async () => { try { const r = await api('/api/keys', { text: keyList.value }); toast(r.count + ' key(s) saved.'); go('settings'); } catch (e) { fail(e); } } }),
      h('button', { class: 'btn', text: 'Test every key', onclick: () => startJob('/api/keys/check') }),
      h('button', { class: 'btn', text: 'Clear invalid / blocked marks', onclick: async () => { try { await api('/api/keys/reset', {}); toast('Marks cleared.'); go('settings'); } catch (e) { fail(e); } } }),
      rot ? h('span', { class: 'mute small', text: `${rot.keys} keys · ${rot.bad} invalid · ${rot.blocked} blocked · ${rot.calls_today} requests today` }) : h('span', { class: 'mute small', text: keys.count < 2 ? 'Off (needs two or more keys).' : '' }))));

  // --- models
  const g = cfg.gemini;
  const mdl = { layout: h('input', { type: 'text', id: 'mLayout', value: g.layout_model }), read: h('input', { type: 'text', id: 'mRead', value: g.extract_models[0] || '' }),
    read2: h('input', { type: 'text', id: 'mRead2', value: g.extract_models[1] || '' }), fill: h('input', { type: 'text', id: 'mFill', value: g.fill_model }),
    fb: h('textarea', { id: 'mFb', rows: 3, value: g.fallback_models.join('\n') }) };
  const suggest = S.suggest;
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Models' }),
    h('p', { class: 'mute', text: 'Google renames and retires models often. “Find working models” tests the likely ones with your key.' }),
    h('div', { class: 'grid g2' }, field('Finds the pages (photos)', mdl.layout), field('Reads the pages', mdl.read), field('Second reader (only for cross-check)', mdl.read2),
      field('AI answers, explanations, math check', mdl.fill)),
    field('Fallback models, one per line (used when a model is out of quota or busy)', mdl.fb),
    suggest ? h('div', { class: 'notice good' }, 'Suggestion from the last scan: reader ' + suggest.extract_models.join(' + ') + ', layout/fill ' + suggest.layout_model + '. ',
      h('button', { class: 'btn small', text: 'Use these', onclick: async () => { await save({ gemini: { layout_model: suggest.layout_model, fill_model: suggest.fill_model, extract_models: suggest.extract_models,
        fallback_models: suggest.fallback_models } }); S.suggest = null; go('settings'); } })) : null,
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('button', { class: 'btn primary', text: 'Save models', onclick: () => save({ gemini: { layout_model: mdl.layout.value, extract_models: [mdl.read.value, mdl.read2.value].filter(Boolean), fill_model: mdl.fill.value,
        fallback_models: mdl.fb.value } }) }),
      h('button', { class: 'btn', text: 'Find working models', onclick: () => startJob('/api/models/scan') }))));

  // --- behaviour
  const c = cfg.checks;
  const num = (id, v, step) => h('input', { type: 'number', id, value: v, min: 0, step: step || 1 });
  const n = { rpm: num('nRpm', g.requests_per_minute), conc: num('nConc', g.concurrency), cap: num('nCap', g.key_daily_cap), halt: num('nHalt', g.halt_after_blocked), dup: num('nDup', c.near_duplicate_ratio, 0.01) };
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'How it works' }),
    check('cAi', 'AI fills gaps', 'Works out answers the source does not give and writes short explanations where there are none. Always labelled.', c.ai_fill),
    check('cProof', 'AI proofreads every page against the photo', 'Corrects misread letters and digits (৪ read as 8, …), broken formulas and ditto marks. 1 request per page. The first reading is kept.', c.proofread),
    check('cMath', 'Math check and repair', 'Tests every formula, lets the AI repair formatting, and reads a picture again when a solution is still broken.', c.math_check),
    check('cCross', 'Read every page twice (cross-check)', 'A second model reads each page and differences are flagged. More accurate, but doubles the requests.', c.cross_check),
    check('cBlock', 'Hold back cards that only have notes', 'Normally only cards with “must check” items wait for review.', c.block_export_on_soft_flags),
    h('div', { class: 'grid g4' }, field('Requests per minute (per model)', n.rpm), field('Parallel requests', n.conc), field('Daily cap per key and model', n.cap), field('Stop if this many keys are blocked', n.halt)),
    h('div', { class: 'grid g4' }, field('Duplicate similarity (0–1)', n.dup)),
    h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn primary', text: 'Save', onclick: () => save({
      checks: { proofread: $('#cProof').checked, ai_fill: $('#cAi').checked, math_check: $('#cMath').checked, cross_check: $('#cCross').checked, block_export_on_soft_flags: $('#cBlock').checked, near_duplicate_ratio: n.dup.value },
      gemini: { requests_per_minute: n.rpm.value, concurrency: n.conc.value, key_daily_cap: n.cap.value, halt_after_blocked: n.halt.value } }) }))));

  // --- anki
  const a = cfg.anki;
  const sl = h('select', { id: 'aSrc' }, [['exam', 'Exam name and year'], ['full', 'Exam, page and question number'], ['none', 'Nothing']].map(([v, t]) => h('option', { value: v, selected: v === a.source_line, text: t })));
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Anki' }),
    h('div', { class: 'grid g3' }, field('Deck name', h('input', { type: 'text', id: 'aDeck', value: a.deck_root }), 'Cards go to “name::subject”.'),
      field('Note type', h('input', { type: 'text', id: 'aNote', value: a.notetype }), 'Must exist in Anki with two fields (Front, Back).'), field('Small grey line on the back', sl)),
    check('aPage', 'Also tag cards with their book page', '', a.page_tag),
    h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn primary', text: 'Save', onclick: () => save({ anki: { deck_root: $('#aDeck').value, notetype: $('#aNote').value, source_line: sl.value, page_tag: $('#aPage').checked } }) }))));

  // --- subjects and photo sources
  const subj = h('textarea', { id: 'subjects', rows: 8, value: Object.entries(cfg.subjects).map(([k, v]) => k + ': ' + v).join('\n') });
  const prof = Object.entries(cfg.profiles).map(([k, v]) => [k, h('input', { type: 'text', value: String(v.rotate ?? 0), 'data-profile': k })]);
  el.append(h('div', { class: 'grid g2', style: 'margin-top:16px' },
    h('div', { class: 'card' }, h('h2', { text: 'Subjects' }), h('p', { class: 'mute small', text: 'One per line, “Name: what it covers”. The names become deck names.' }), subj,
      h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn primary', text: 'Save subjects', onclick: () => {
        const map = {};
        subj.value.split('\n').forEach((line) => { const i = line.indexOf(':'); if (i > 0) map[line.slice(0, i).trim()] = line.slice(i + 1).trim(); });
        save({ subjects: map }); } }))),
    h('div', { class: 'card' }, h('h2', { text: 'Photo types' }), h('p', { class: 'mute small', text: 'How many degrees counter-clockwise each type needs to turn upright (0, 90, 180, 270 or auto).' }),
      prof.map(([k, input]) => field(k + ' — ' + (PROFILE_HELP[k] || ''), input)),
      h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn primary', text: 'Save', onclick: () => {
        const rot2 = {}; prof.forEach(([k, input]) => { rot2[k] = input.value; }); save({ profile_rotate: rot2 }); } })))));

  el.append(h('div', { class: 'row', style: 'margin-top:18px' }, h('button', { class: 'btn danger', text: 'Reset all settings to the defaults', onclick: async () => {
    if (!confirm('Go back to the default settings? (Your key and the key list are not touched.)')) return;
    try { await api('/api/settings/reset', {}); toast('Settings reset.'); go('settings'); } catch (e) { fail(e); } } }),
    h('span', { class: 'mute small', text: cfg.has_overrides ? 'You have changed some settings.' : 'Everything is at its default.' })));
}

/* ------------------------------------------------------------------ Database */
async function viewDatabase(el) {
  const st = await refreshStatus();
  const [backups, cfg] = await Promise.all([api('/api/backups'), api('/api/settings')]);
  const s = st.stats;
  el.append(head('Database', 'Your cards live in one file. Back it up before big changes.'));
  el.append(h('div', { class: 'grid g4' }, stat(s.photos, 'photos read'), stat((s.ok || 0) + (s.approved || 0), 'good cards'), stat(s.review || 0, 'waiting for review'), stat(s.duplicates_merged, 'duplicates merged')));

  const backupBtn = h('button', { class: 'btn primary', text: 'Back up now', onclick: async () => { try { const r = await api('/api/db/backup', {}); toast('Saved ' + r.name); go('database'); } catch (e) { fail(e); } } });
  const autoB = h('input', { type: 'checkbox', id: 'bAuto', checked: cfg.backup.auto });
  const keepB = h('input', { type: 'number', id: 'bKeep', min: 1, value: cfg.backup.keep, style: 'width:90px' });
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Automatic backup' }),
    h('label', { class: 'chk' }, autoB, h('span', {}, 'Back up automatically before every task that changes cards', h('small', { text: 'Reading photos, rebuilding, AI fill and proofreading. Your own backups are never deleted.' }))),
    h('div', { class: 'row' }, h('span', { text: 'Keep the newest' }), keepB, h('span', { text: 'automatic backups' }),
      h('button', { class: 'btn primary', text: 'Save', onclick: async () => { try { await api('/api/settings', { backup: { auto: autoB.checked, keep: keepB.value } }); toast('Saved.'); refreshStatus(); } catch (e) { fail(e); } } }))));
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Backups' }), h('div', { class: 'row' }, backupBtn,
    h('button', { class: 'btn', text: 'Open backups folder', onclick: () => api('/api/open', { what: 'backups' }).catch(fail) })),
    backups.length ? h('table', { style: 'margin-top:10px' }, h('tr', {}, ['file', 'when', 'size', ''].map((t) => h('th', { text: t }))), backups.map((b) => h('tr', {}, h('td', { text: b.name }), h('td', { text: b.time }),
      h('td', { text: Math.round(b.size / 1024) + ' KB' }), h('td', {}, h('button', { class: 'btn small', text: 'Restore', onclick: async () => {
        if (!confirm('Restore ' + b.name + '? The current database is backed up first.')) return;
        try { await api('/api/db/restore', { name: b.name }); toast('Restored.'); go('database'); } catch (e) { fail(e); } } }))))) : h('p', { class: 'mute', text: 'No backups yet.' })));

  const bundleOut = h('span', { class: 'mute small' });
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Tools' }),
    h('div', { class: 'row' },
      h('button', { class: 'btn', text: 'Rebuild from saved answers', onclick: () => startJob('/api/rebuild', {}) }),
      h('button', { class: 'btn', text: 'Let the AI fill gaps in stored cards', onclick: () => startJob('/api/fill', {}) }),
      h('button', { class: 'btn', text: 'Proofread stored cards', onclick: () => startJob('/api/proofread', {}) }),
      h('button', { class: 'btn', text: 'Make a debug bundle', onclick: async () => { try { const r = await api('/api/bundle', {}); bundleOut.textContent = ''; bundleOut.append(h('a', { href: '/download/' + r.file, text: r.file }), ' (never contains your keys)'); } catch (e) { fail(e); } } }), bundleOut),
    h('p', { class: 'mute small', text: 'Rebuild re-applies the current rules to photos already read, using answers saved earlier; new steps such as proofreading make new requests. Photos with approved or exported cards are skipped. Proofread stored cards checks cards read before against their page pictures (1 request per page); approved cards are not touched.' }),
    h('div', { class: 'row' }, ['inbox', 'out', 'data', 'work', 'root'].map((w) => h('button', { class: 'btn small', text: 'Open ' + (w === 'root' ? 'app' : w) + ' folder', onclick: () => api('/api/open', { what: w }).catch(fail) })))));

  const reset = (mode, text, need) => h('button', { class: 'btn danger', text, onclick: async () => {
    let confirmText;
    if (need) { confirmText = prompt('This cannot be undone (a backup is kept for the cards). Type DELETE to continue:'); if (confirmText !== 'DELETE') return; }
    else if (!confirm('Mark every card as not exported yet?')) return;
    try { const r = await api('/api/db/reset', { mode, confirm: confirmText }); toast(r.message); go('database'); } catch (e) { fail(e); } } });
  el.append(h('div', { class: 'card', style: 'margin-top:16px' }, h('h2', { text: 'Start over' }),
    h('p', { class: 'mute', text: 'Your photos in the inbox, your settings and your keys are never touched.' }),
    h('div', { class: 'row' }, reset('exported', 'Allow re-exporting every card', false), reset('records', 'Forget all cards', true), reset('everything', 'Delete everything incl. saved AI answers', true)),
    h('p', { class: 'mute small', text: '“Forget all cards” keeps the saved AI answers: reading the photos again costs no requests. “Delete everything” removes them too, so every photo goes to the AI again.' })));
}

/* ------------------------------------------------------------------ Help */
async function viewHelp(el) {
  el.append(head('Help', 'The short version. The README.md in the app folder has the full guide.'));
  const sec = (t, ...kids) => h('div', { class: 'card', style: 'margin-bottom:16px' }, h('h2', { text: t }), ...kids);
  el.append(
    sec('Every day', h('ol', { class: 'steps' },
      h('li', { text: 'Read photos: add your photos, then press “Read waiting photos”.' }),
      h('li', { text: 'Review: open each card that needs review, compare it with the photo, press Ctrl+Enter to approve.' }),
      h('li', { text: 'Export to Anki: create the file and import it into Anki (once per file).' }))),
    sec('What the colours mean', h('p', {}, chip('must check', 'hard'), ' the app is not sure; the card waits until you approve it. ', chip('note', 'soft'), ' worth a glance; the card is exported anyway. No label: exported as it is.'),
      h('p', { class: 'mute', text: 'Answers the AI had to work out are marked “answer by AI” and tagged Answer::AI in Anki, so you can find and re-check them.' })),
    sec('Photo types', h('p', { text: 'Leave the type on “Detect automatically”: the app works out whether a photo is a book spread, a single page or a screenshot, and turns it upright (1 small request per photo). You can still choose “book” or “screenshot” yourself. An exam name you type names the exam when an image has no title.' })),
    sec('Quotas and limits', h('p', { text: 'Free Google keys allow only a few dozen requests per model per day. When the daily quota is used up the app stops cleanly and tells you when to continue (midnight Pacific time). Nothing is lost.' }),
      h('p', { text: 'Fallback models (Settings → Models) are tried automatically when one is out of quota or busy.' })),
    sec('Something looks wrong?', h('ul', { class: 'steps' },
      h('li', { text: 'A photo failed: see the reason in the Inbox list; fix it (flatter photo, right turn) and read again.' }),
      h('li', { text: '“model not found”: Settings → Models → Find working models.' }),
      h('li', { text: 'Anki says “notetype was missing”: create a note type named exactly like the one in Settings → Anki.' }),
      h('li', { text: 'Need help: Database → Make a debug bundle, and send the file.' }))));
}

/* ------------------------------------------------------------------ start */
window.addEventListener('hashchange', () => { const v = location.hash.slice(1); if (v && v !== S.view && VIEWS.some((x) => x[0] === v)) go(v); });
(async () => {
  renderNav();
  await refreshStatus();
  const first = location.hash.slice(1);
  go(VIEWS.some((x) => x[0] === first) ? first : 'home');
  pollJob();
  setInterval(refreshStatus, 8000);
})();
})();
