/* RelayDrop front-end (vanilla JS). Talks to the Python API in backend/relaydrop/api.py.
   The API address comes from js/config.js (window.RELAYDROP_API; empty = same origin). */
(() => {
  'use strict';

  /* ------------------------------------------------------------------ helpers */
  const MIB = 1024 * 1024;
  const SENDER_HEADER = 'X-Relay-Sender';
  const STORAGE_KEY = 'relaydrop.sender.v1';
  const TOKEN_RE = /^[a-f0-9]{64}$/;
  const EXPIRY = [{ value: 1, label: '1 hour' }, { value: 24, label: '24 hours' }, { value: 72, label: '3 days' }, { value: 168, label: '7 days' }];

  const API = String((window.RELAYDROP_CONFIG && window.RELAYDROP_CONFIG.apiBase) || '').replace(/\/+$/, '');
  const api = path => API + path;
  const CROSS_SITE = !!API && new URL(API, location.href).origin !== location.origin;
  const CREDENTIALS = CROSS_SITE ? 'include' : 'same-origin';
  const SITE = location.origin + location.pathname.replace(/[^/]*$/, ''); // folder this page lives in (works under /repo-name/ on GitHub Pages)

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (name, size = 24, stroke = 2, cls = '') =>
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${cls ? ` class="${cls}"` : ''}>${(window.RD_ICONS || {})[name] || ''}</svg>`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const fmtSize = v => { if (!v) return '0 B'; if (v < 1024) return `${v} B`; if (v < MIB) return `${(v / 1024).toFixed(1)} KiB`; return `${(v / MIB).toFixed(1)} MiB`; };
  const fmtDate = v => new Date(v).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const errMsg = e => (e instanceof Error ? e.message : 'Something went wrong. Please try again.');
  const alertBox = (msg, success = false) => `<div role="${success ? 'status' : 'alert'}" class="notice ${success ? 'notice-success' : 'notice-error'}">${icon(success ? 'check' : 'help', 17)}<span>${esc(msg)}</span></div>`;
  const spinner = (size = 18) => icon('loader', size, 2, 'spin');
  const fileIconName = name => {
    const ext = (name.split('.').pop() || '').toLowerCase();
    if (['jpg', 'jpeg', 'png', 'webp', 'gif', 'svg', 'heic'].includes(ext)) return 'file-image';
    if (['mp4', 'mov', 'webm', 'avi'].includes(ext)) return 'file-video';
    if (['mp3', 'wav', 'm4a', 'ogg'].includes(ext)) return 'file-audio';
    if (['zip', 'rar', '7z', 'gz'].includes(ext)) return 'file-archive';
    if (['pdf', 'txt', 'doc', 'docx', 'md', 'csv'].includes(ext)) return 'file-text';
    return 'file';
  };
  const fileIcon = name => icon(fileIconName(name), 20, 1.7);
  const bind = (root, sel, evt, fn) => $$(sel, root).forEach(el => el.addEventListener(evt, fn));

  /* ------------------------------------------------------------------ browser session (anonymous sender token) */
  let bootstrap = null;
  function savedToken() {
    try {
      const v = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (v && TOKEN_RE.test(v.token) && typeof v.expiresAt === 'number' && v.expiresAt > Date.now()) return v.token;
    } catch (_) { /* storage unavailable: the in-memory token still works */ }
    return undefined;
  }
  function saveToken(token) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ token, expiresAt: Date.now() + 30 * 24 * 3600 * 1000 })); } catch (_) { /* ignore */ }
  }
  async function bootstrapSender() {
    const headers = new Headers();
    const previous = savedToken();
    if (previous) headers.set(SENDER_HEADER, previous);
    const res = await fetch(api('/api/session'), { headers, credentials: CREDENTIALS, cache: 'no-store' });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && typeof data.error === 'string') ? data.error : 'Unable to prepare your browser for file sharing. Please try again.');
    if (!data || !TOKEN_RE.test(data.sender_token || '')) throw new Error('The server could not create a valid sharing session. Please try again.');
    saveToken(data.sender_token);
    return data.sender_token;
  }
  const getSenderToken = () => (bootstrap ||= bootstrapSender().catch(e => { bootstrap = null; throw e; }));

  async function request(url, options = {}) {
    const token = await getSenderToken();
    const headers = new Headers(options.headers);
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    headers.set(SENDER_HEADER, token);
    const res = await fetch(api(url), { ...options, headers, credentials: CREDENTIALS, cache: 'no-store' });
    const data = await res.json().catch(() => ({ error: 'The server did not respond. Please try again.' }));
    if (!res.ok) throw Object.assign(new Error(data.error || 'Unable to complete this request.'), { needsPassword: data.needs_password });
    return data;
  }
  async function requestBlob(url, payload) {
    const token = await getSenderToken();
    const res = await fetch(api(url), { method: 'POST', headers: { 'Content-Type': 'application/json', [SENDER_HEADER]: token }, body: JSON.stringify(payload), credentials: CREDENTIALS, cache: 'no-store' });
    if (!res.ok) throw new Error('Image could not be generated.');
    return URL.createObjectURL(await res.blob());
  }
  const jsonBody = obj => JSON.stringify(obj);

  /* ------------------------------------------------------------------ header (mobile menu) */
  $$('[data-year]').forEach(el => { el.textContent = new Date().getFullYear(); });
  const menuBtn = $('#mobile-menu'), nav = $('#main-nav');
  if (menuBtn && nav) {
    menuBtn.addEventListener('click', () => {
      const open = !nav.classList.contains('nav-open');
      nav.classList.toggle('nav-open', open);
      menuBtn.setAttribute('aria-expanded', String(open));
      menuBtn.innerHTML = icon(open ? 'x' : 'menu', 22);
    });
    $$('a', nav).forEach(a => a.addEventListener('click', () => { nav.classList.remove('nav-open'); menuBtn.setAttribute('aria-expanded', 'false'); menuBtn.innerHTML = icon('menu', 22); }));
  }

  /* ------------------------------------------------------------------ HOME: send / receive / history */
  const panel = $('#panel');
  if (panel) initTransfers();

  function initTransfers() {
    const S = {
      tab: 'send',
      files: [], title: '', note: '', noteOpen: false, hours: 24, password: '', passwordOn: false, dragging: false,
      busy: false, progress: 0, phase: '', error: '', result: null, assets: null, copied: '', assetError: '', barcodeOpen: false,
      reference: '', receivePassword: '', needsPassword: false, received: null, receiveBusy: false, receiveError: '',
      history: [], historyBusy: false, historyError: '', revokeId: null, revokeBusy: false,
    };
    const total = () => S.files.reduce((s, f) => s + f.size, 0);

    function setTab(tab) {
      S.tab = tab;
      $$('.tab').forEach(b => { const on = b.dataset.tab === tab; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
      if (tab === 'history') loadHistory(); else render();
    }
    $$('.tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
    const hh = $('#header-history');
    if (hh) hh.addEventListener('click', () => { setTab('history'); $('#transfer-workspace').scrollIntoView({ behavior: 'smooth', block: 'start' }); });

    /* ---- actions */
    function addFiles(incoming) {
      S.error = '';
      if (!incoming.length) return render();
      const next = [...S.files];
      for (const f of incoming) if (!next.some(x => x.name === f.name && x.size === f.size && x.lastModified === f.lastModified)) next.push(f);
      let err = '';
      if (next.length > 20) err = 'You can send up to 20 files at once. Remove a few files and try again.';
      else if (next.some(f => f.size > 50 * MIB)) err = 'Each file must be 50 MiB or smaller. Please choose smaller files.';
      else if (next.some(f => f.size === 0)) err = 'Empty files cannot be transferred. Please choose a file with content.';
      else if (next.reduce((s, f) => s + f.size, 0) > 100 * MIB) err = 'This transfer exceeds 100 MiB. Remove a file or split it into two transfers.';
      if (err) { S.error = err; return render(); }
      S.files = next; S.result = null; render();
    }

    function uploadFile(url, file, completed, totalBytes, token) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', api(url));
        xhr.withCredentials = CROSS_SITE;
        xhr.setRequestHeader(SENDER_HEADER, token);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.upload.onprogress = e => { if (e.lengthComputable) setProgress(Math.min(99, Math.round(((completed + e.loaded) / totalBytes) * 100))); };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) return resolve();
          let message = 'This file could not be uploaded. Please try again.';
          try { message = JSON.parse(xhr.responseText).error || message; } catch (_) { /* keep default */ }
          reject(new Error(message));
        };
        xhr.onerror = () => reject(new Error('Connection interrupted. Check your internet connection and try again.'));
        xhr.timeout = 180000;
        xhr.ontimeout = () => reject(new Error('Upload timed out. Check your connection and try again.'));
        xhr.send(file);
      });
    }
    function setProgress(p) {
      S.progress = p;
      const bar = $('#prog-bar'), pct = $('#prog-pct');
      if (bar) bar.style.width = p + '%';
      if (pct) pct.textContent = p + '%';
    }
    function setPhase(text) { S.phase = text; const el = $('#prog-phase'); if (el) el.textContent = text; }

    async function send() {
      if (!S.files.length) { $('#file-input')?.click(); return; }
      if (S.passwordOn && S.password.length < 8) { S.error = 'Use a password with at least 8 characters.'; return render(); }
      S.error = ''; S.busy = true; S.progress = 0; S.phase = 'Preparing your transfer…'; render();
      let draftId = null;
      try {
        const draft = await request('/api/transfers', { method: 'POST', body: jsonBody({ files: S.files.map(f => ({ name: f.name, size: f.size, type: f.type })), hours: S.hours, title: S.title, note: S.note, password: S.passwordOn ? S.password : '' }) });
        draftId = draft.id;
        const token = await getSenderToken();
        const totalBytes = total();
        let completed = 0;
        for (let i = 0; i < S.files.length; i++) {
          setPhase(`Uploading ${i + 1} of ${S.files.length}: ${S.files[i].name}`);
          await uploadFile(`/api/transfers/${encodeURIComponent(draft.id)}/files/${encodeURIComponent(draft.files[i].id)}`, S.files[i], completed, totalBytes, token);
          completed += S.files[i].size;
        }
        setPhase('Finishing your transfer…');
        const transfer = await request(`/api/transfers/${encodeURIComponent(draft.id)}/finalize`, { method: 'POST' });
        S.progress = 100; S.result = transfer; S.assets = null; S.copied = ''; S.assetError = '';
        S.files = []; S.password = ''; S.passwordOn = false; S.title = ''; S.note = ''; S.noteOpen = false;
      } catch (err) {
        S.error = errMsg(err);
        if (draftId) request(`/api/transfers/${encodeURIComponent(draftId)}`, { method: 'DELETE' }).catch(() => undefined);
      } finally { S.busy = false; render(); }
    }

    async function receive(value = S.reference, pass = S.receivePassword) {
      if (!value.trim()) { S.receiveError = 'Enter an 8-digit transfer code or a sharing link.'; return render(); }
      S.receiveError = ''; S.receiveBusy = true; S.received = null; render();
      try {
        S.received = await request('/api/receive', { method: 'POST', body: jsonBody({ reference: value.trim(), password: pass }) });
        S.needsPassword = false;
      } catch (err) {
        if (err.needsPassword) { S.needsPassword = true; S.receiveError = pass ? 'That password did not match. Please try again.' : 'This transfer is password protected. Ask the sender for its password.'; }
        else S.receiveError = errMsg(err);
      } finally { S.receiveBusy = false; render(); }
    }

    async function loadHistory() {
      S.historyBusy = true; S.historyError = ''; render();
      try { S.history = (await request('/api/transfers')).transfers; } catch (err) { S.historyError = errMsg(err); }
      finally { S.historyBusy = false; render(); }
    }
    async function revoke() {
      if (!S.revokeId) return;
      S.revokeBusy = true; S.historyError = ''; render();
      try { await request(`/api/transfers/${encodeURIComponent(S.revokeId)}`, { method: 'DELETE' }); S.revokeId = null; S.revokeBusy = false; await loadHistory(); }
      catch (err) { S.historyError = errMsg(err); S.revokeBusy = false; render(); }
    }
    async function copy(text, key) {
      try { await navigator.clipboard.writeText(text); S.copied = key; render(); setTimeout(() => { if (S.copied === key) { S.copied = ''; render(); } }, 2000); }
      catch (_) { S.assetError = 'Clipboard access is unavailable. Select and copy the link or code below.'; render(); }
    }
    function loadAssets(t) {
      if (S.assets && S.assets.id === t.id) return;
      const url = `${SITE}index.html?receive=${encodeURIComponent(t.share_token)}`;
      S.assets = { id: t.id, url, qr: '', barcode: '' };
      requestBlob('/api/qr', { text: url, format: 'png' }).then(u => { S.assets.qr = u; render(); }).catch(() => { S.assetError = 'The QR image could not be generated. You can still share the link or code.'; render(); });
      requestBlob('/api/barcode', { code: t.code, format: 'png' }).then(u => { S.assets.barcode = u; render(); }).catch(() => { S.assetError = 'The barcode could not be generated. You can still share the link or code.'; render(); });
    }

    /* ---- views */
    function render() {
      panel.innerHTML = S.tab === 'receive' ? receiveView() : S.tab === 'history' ? historyView() : S.result ? shareView() : sendView();
      wire();
    }

    function sendView() {
      const f = S.files, dis = S.busy ? 'disabled' : '';
      return `<div class="send-view"><div class="panel-title"><h2>A fresh handoff.</h2><p>All your files, one simple link.</p></div>
<input id="file-input" type="file" multiple class="visually-hidden" tabindex="-1" ${dis}>
<button id="dropzone" class="dropzone ${S.dragging ? 'dragging' : ''} ${f.length ? 'has-files' : ''}" type="button" ${dis}><span class="upload-icon">${icon('upload', 25, 1.6)}<span class="upload-plus">${icon('plus', 11)}</span></span><strong>${f.length ? 'Add a few more?' : 'Drop your files here'}</strong><span>or <b>browse files</b> on your device</span><small>20 files · 50 MiB per file · 100 MiB total</small></button>
${f.length ? `<div class="selected-files"><div class="file-summary"><span>${plural(f.length, 'file', 'files')} selected</span><span>${fmtSize(total())} / 100 MiB</span></div>${f.map((file, i) => `<div class="selected-file"><span class="file-icon">${fileIcon(file.name)}</span><span class="file-info"><b>${esc(file.name)}</b><small>${fmtSize(file.size)}</small></span><button class="icon-button" type="button" data-remove="${i}" aria-label="Remove ${esc(file.name)}" ${dis}>${icon('x', 16)}</button></div>`).join('')}</div>` : ''}
<div class="transfer-options"><div class="field"><label for="transfer-title">Give it a name <span>Optional</span></label><input id="transfer-title" value="${esc(S.title)}" placeholder="e.g. The final-final files" maxlength="120" ${dis}></div>
<div class="field"><label for="transfer-expiry">${icon('clock', 14)} Link expires after</label><div class="select-wrap"><select id="transfer-expiry" ${dis}>${EXPIRY.map(o => `<option value="${o.value}" ${o.value === S.hours ? 'selected' : ''}>${o.label}</option>`).join('')}</select>${icon('chevron-down', 16)}</div></div></div>
<div class="password-option"><label for="password-toggle">${icon('lock', 16)}<span>Password protection</span><span class="optional-label">Optional</span></label><button id="password-toggle" role="switch" aria-checked="${S.passwordOn}" aria-label="Password protection" type="button" ${dis} class="switch ${S.passwordOn ? 'switch-on' : ''}"><span></span></button></div>
${S.passwordOn ? `<div class="field password-field"><label for="transfer-password">Create a password</label><input id="transfer-password" type="password" value="${esc(S.password)}" placeholder="At least 8 characters" minlength="8" maxlength="128" autocomplete="new-password" ${dis}><p class="form-hint">Send the password to your recipient separately.</p></div>` : ''}
<details class="note-details" ${S.noteOpen ? 'open' : ''}><summary>Add a message <span>Optional</span>${icon('plus', 14)}</summary><textarea id="transfer-note" aria-label="Message to recipient" placeholder="A little context for your recipient…" maxlength="1000" rows="3" ${dis}>${esc(S.note)}</textarea></details>
${S.error ? alertBox(S.error) : ''}
${S.busy ? `<div class="upload-progress" aria-live="polite"><div><span id="prog-phase">${esc(S.phase)}</span><b id="prog-pct">${S.progress}%</b></div><div class="progress-track"><span id="prog-bar" style="width:${S.progress}%"></span></div></div>` : ''}
<button id="send-btn" type="button" class="button button-primary wide send-button" ${dis}>${S.busy ? `${spinner()}Sending your files…` : `Create transfer ${icon('arrow-up-right', 18)}`}</button>
<p class="terms-note">By sending files, you agree to our <a href="terms.html">Terms</a> and <a href="privacy.html">Privacy Policy</a>.</p></div>`;
    }

    function shareView() {
      const t = S.result; loadAssets(t);
      const a = S.assets, c = t.code, copied = S.copied;
      return `<div class="share-view"><div class="share-success"><span>${icon('check', 28)}</span><div class="eyebrow">READY WHEN THEY ARE</div><h2>Your transfer is ready.</h2><p>Pass along the link or code. We’ll take it from here.</p></div>
<div class="share-transfer-info">${icon('file-archive', 19)}<div><b>${esc(t.title || 'Your new transfer')}</b><small>${plural(t.file_count, 'file', 'files')} · ${fmtSize(t.total_size)} · Expires ${fmtDate(t.expires_at)}</small></div>${t.protected ? icon('lock', 17) : ''}</div>
<label class="share-label" for="share-link">Your sharing link</label><div class="share-link-box"><input readonly id="share-link" value="${esc(a.url)}"><button class="icon-button" type="button" data-copy="link" aria-label="Copy sharing link">${icon(copied === 'link' ? 'check' : 'copy', 17)}</button></div>
<div class="code-sharing"><div><span class="share-label">Or share this 8-digit code</span><button class="big-code" type="button" data-copy="code" aria-label="Copy code ${esc(c)}">${esc(c.slice(0, 4))} <span>${esc(c.slice(4))}</span>${icon(copied === 'code' ? 'check' : 'copy', copied === 'code' ? 18 : 17)}</button></div>
<div class="qr-code">${a.qr ? `<img src="${a.qr}" width="100" height="100" alt="QR code for this transfer"><a href="${a.qr}" download="RelayDrop-${esc(c)}-QR.png">Save QR ${icon('download', 11)}</a>` : ''}</div></div>
<details class="barcode-details" ${S.barcodeOpen ? 'open' : ''}><summary>${icon('qr', 14)}Need a barcode?${icon('chevron-down', 13)}</summary><div>${a.barcode ? `<img src="${a.barcode}" alt="Code 128 barcode for ${esc(c)}"><a class="text-link" href="${a.barcode}" download="RelayDrop-${esc(c)}-barcode.png">Save barcode ${icon('download', 14)}</a>` : ''}</div></details>
${S.assetError ? alertBox(S.assetError) : ''}
${t.protected ? `<p class="form-hint">${icon('lock', 13)}Remember to share the password separately.</p>` : ''}
<div aria-live="polite" class="copy-status">${copied ? `${copied === 'link' ? 'Link' : 'Code'} copied to clipboard.` : ''}</div>
<button class="button button-primary wide" type="button" data-copy="link">${icon(copied === 'link' ? 'check' : 'copy', 17)} ${copied === 'link' ? 'Link copied' : 'Copy sharing link'}</button>
<button class="button button-quiet wide" type="button" id="new-transfer">${icon('plus', 15)}Send something else</button></div>`;
    }

    function receiveView() {
      const r = S.received;
      const dl = q => `${api('/download')}?token=${encodeURIComponent(r.download_token)}${q}`;
      return `<div class="receive-view"><div class="panel-title"><div class="eyebrow">A DELIVERY FOR YOU</div><h2>Good things incoming.</h2><p>Enter the code or link your sender shared with you.</p></div>
<form id="receive-form" class="receive-form"><label for="receive-code">Transfer code or sharing link</label><div class="input-with-icon">${icon('link', 18)}<input id="receive-code" value="${esc(S.reference)}" placeholder="1234 5678 or paste a link" required maxlength="500"></div>
${S.needsPassword ? `<div class="field"><label for="receive-password">Transfer password</label><input autofocus id="receive-password" type="password" value="${esc(S.receivePassword)}" required autocomplete="off"></div>` : ''}
${S.receiveError ? alertBox(S.receiveError) : ''}
<button class="button button-primary wide" ${S.receiveBusy ? 'disabled' : ''}>${S.receiveBusy ? spinner() : icon('arrow-down-to-line', 18)} ${S.receiveBusy ? 'Finding your files…' : S.needsPassword ? 'Unlock transfer' : 'Find my files'}</button></form>
${r ? `<div class="received-card"><div class="received-header"><span class="success-icon">${icon('check-check', 21)}</span><div><h3>${esc(r.transfer.title || 'Your files are ready')}</h3><p>${plural(r.files.length, 'file', 'files')} · ${fmtSize(r.transfer.total_size)}</p></div></div>
${r.transfer.note ? `<p class="sender-note">${esc(r.transfer.note)}</p>` : ''}
<div class="receive-file-list">${r.files.map(f => `<a class="receive-file" href="${dl('&file_id=' + encodeURIComponent(f.id))}"><span class="file-icon">${fileIcon(f.name)}</span><span class="file-info"><b>${esc(f.name)}</b><small>${fmtSize(f.size)}</small></span>${icon('download', 17)}</a>`).join('')}</div>
<a class="button button-primary wide" href="${dl('')}">${icon('file-archive', 17)}Download all as ZIP</a><p class="form-hint">${icon('clock', 13)}Available until ${fmtDate(r.transfer.expires_at)}</p><a class="report-link" href="report.html?reference=${encodeURIComponent(r.transfer.code)}">Report this transfer</a></div>`
        : `<div class="receive-placeholder"><span class="receive-orbit">${icon('folder-open', 34, 1.4)}</span><p>Your next handoff, one code away.</p><small>Only open files from people you trust.</small></div>`}</div>`;
    }

    function historyView() {
      const now = Date.now();
      let body;
      if (S.historyBusy) body = `<div class="empty-state">${spinner(25)}<p>Loading your transfers…</p></div>`;
      else if (!S.history.length) body = `<div class="empty-state"><span class="empty-icon">${icon('folder-open', 32)}</span><h3>Your first handoff starts here.</h3><p>Once you send files, you can find their links and revoke access here. Keep this browser’s site data to manage anonymous transfers.</p><button class="button button-primary" type="button" id="first-send">Send your first files ${icon('arrow-up-right', 17)}</button></div>`;
      else body = `<div class="history-list">${S.history.map(item => {
        const active = item.status === 'ready' && item.expires_at > now;
        const label = active ? 'Available' : item.status === 'revoked' ? 'Revoked' : item.expires_at <= now ? 'Expired' : item.status;
        return `<div class="history-item"><div class="history-item-top"><span class="file-icon">${icon('file-archive', 20)}</span><div class="file-info"><h3>${esc(item.title || 'Untitled transfer')}</h3><p>${plural(item.file_count, 'file', 'files')} · ${fmtSize(item.total_size)} · ${fmtDate(item.created_at)}</p></div><span class="status-badge ${active ? 'is-active' : ''}">${esc(label)}</span></div>
<div class="history-item-bottom"><span class="code-small">${esc(item.code)}</span><span class="history-expiry">${icon('clock', 12)}${active ? 'Until ' : ''}${fmtDate(item.expires_at)}</span>${active ? `<button class="text-link" type="button" data-share="${esc(item.id)}">Share ${icon('arrow-up-right', 13)}</button><button aria-label="Revoke ${esc(item.title || item.code)}" class="icon-button danger" type="button" data-revoke="${esc(item.id)}">${icon('trash', 15)}</button>` : ''}</div></div>`;
      }).join('')}</div>`;
      return `<div class="history-view"><div class="panel-title"><div class="eyebrow">YOUR RECENT HANDOFFS</div><div class="title-action"><h2>My transfers</h2><button class="text-link" type="button" id="refresh-history" ${S.historyBusy ? 'disabled' : ''}>Refresh ${icon('history', 14)}</button></div><p>Manage transfers created with your current account or browser.</p></div>
${S.historyError ? alertBox(S.historyError) : ''}${body}
${S.revokeId ? `<div class="confirm-box"><h3>Revoke access to this transfer?</h3><p>The sharing link and code will stop working. People who already downloaded the files will keep their copies.</p><div class="button-row"><button class="button button-danger" type="button" id="do-revoke" ${S.revokeBusy ? 'disabled' : ''}>${S.revokeBusy ? 'Revoking…' : 'Revoke access'}</button><button class="button button-outline" type="button" id="cancel-revoke" ${S.revokeBusy ? 'disabled' : ''}>Keep transfer</button></div></div>` : ''}</div>`;
    }

    /* ---- event wiring after each render */
    function wire() {
      const p = panel;
      // send view
      const input = $('#file-input', p), dz = $('#dropzone', p);
      if (input) input.addEventListener('change', e => { addFiles(Array.from(e.target.files || [])); });
      if (dz) {
        dz.addEventListener('click', () => input.click());
        dz.addEventListener('dragover', e => { e.preventDefault(); if (!S.dragging) { S.dragging = true; dz.classList.add('dragging'); } });
        dz.addEventListener('dragleave', () => { S.dragging = false; dz.classList.remove('dragging'); });
        dz.addEventListener('drop', e => { e.preventDefault(); S.dragging = false; if (!S.busy) addFiles(Array.from(e.dataTransfer.files)); else dz.classList.remove('dragging'); });
      }
      bind(p, '[data-remove]', 'click', e => { S.files = S.files.filter((_, i) => i !== Number(e.currentTarget.dataset.remove)); render(); });
      const title = $('#transfer-title', p); if (title) title.addEventListener('input', e => { S.title = e.target.value; });
      const exp = $('#transfer-expiry', p); if (exp) exp.addEventListener('change', e => { S.hours = Number(e.target.value); });
      const pwt = $('#password-toggle', p); if (pwt) pwt.addEventListener('click', () => { S.passwordOn = !S.passwordOn; render(); });
      const pw = $('#transfer-password', p); if (pw) pw.addEventListener('input', e => { S.password = e.target.value; });
      const note = $('#transfer-note', p); if (note) note.addEventListener('input', e => { S.note = e.target.value; });
      const nd = $('.note-details', p); if (nd) nd.addEventListener('toggle', () => { S.noteOpen = nd.open; });
      const sb = $('#send-btn', p); if (sb) sb.addEventListener('click', send);
      // share view
      bind(p, '[data-copy]', 'click', e => copy(e.currentTarget.dataset.copy === 'link' ? S.assets.url : S.result.code, e.currentTarget.dataset.copy));
      const sl = $('#share-link', p); if (sl) sl.addEventListener('focus', e => e.target.select());
      const bd = $('.barcode-details', p); if (bd) bd.addEventListener('toggle', () => { S.barcodeOpen = bd.open; });
      const nt = $('#new-transfer', p); if (nt) nt.addEventListener('click', () => { S.result = null; S.error = ''; S.assetError = ''; render(); });
      // receive view
      const rc = $('#receive-code', p); if (rc) rc.addEventListener('input', e => { S.reference = e.target.value; S.received = null; S.needsPassword = false; });
      const rp = $('#receive-password', p); if (rp) { rp.addEventListener('input', e => { S.receivePassword = e.target.value; }); rp.focus(); }
      const rf = $('#receive-form', p); if (rf) rf.addEventListener('submit', e => { e.preventDefault(); receive(); });
      // history view
      const rh = $('#refresh-history', p); if (rh) rh.addEventListener('click', loadHistory);
      const fs = $('#first-send', p); if (fs) fs.addEventListener('click', () => setTab('send'));
      bind(p, '[data-share]', 'click', e => { S.result = S.history.find(h => h.id === e.currentTarget.dataset.share); S.assets = null; setTab('send'); });
      bind(p, '[data-revoke]', 'click', e => { S.revokeId = e.currentTarget.dataset.revoke; render(); });
      const dr = $('#do-revoke', p); if (dr) dr.addEventListener('click', revoke);
      const cr = $('#cancel-revoke', p); if (cr) cr.addEventListener('click', () => { S.revokeId = null; render(); });
    }

    // initial state from the URL (?receive=<link or code>, ?tab=history)
    const params = new URLSearchParams(location.search);
    const incoming = params.get('receive');
    if (incoming) { S.reference = incoming; setTab('receive'); receive(incoming, ''); }
    else if (params.get('tab') === 'history') setTab('history');
    else render();
  }

  /* ------------------------------------------------------------------ SUPPORT: contact / report form */
  const support = $('#support-root');
  if (support) initSupport(support);

  function initSupport(root) {
    const kind = root.dataset.kind, isReport = kind === 'report';
    const q = new URLSearchParams(location.search);
    const S = { ticketKind: isReport ? (q.get('kind') === 'copyright' ? 'copyright' : 'abuse') : 'contact', name: '', email: '', subject: '', message: '', reference: q.get('reference') || '',
      declarations: false, signature: '', busy: false, error: '', receipt: null, receiptURL: '', existing: null, checking: false };

    async function submit(e) {
      e.preventDefault(); S.busy = true; S.error = ''; render();
      try {
        const data = await request('/api/tickets', { method: 'POST', body: jsonBody({ kind: S.ticketKind, name: S.name, email: S.email, subject: S.subject, message: S.message, reference: S.reference, declarations: S.declarations, signature: S.signature }) });
        S.receipt = data; S.receiptURL = `${SITE}contact.html?ticket=${encodeURIComponent(data.id)}&receipt=${encodeURIComponent(data.receipt)}`;
      } catch (err) { S.error = errMsg(err); } finally { S.busy = false; render(); }
    }

    function render() {
      if (S.checking) root.innerHTML = `<div class="empty-state">${spinner(24)}<p>Checking your request…</p></div>`;
      else if (S.existing) {
        const x = S.existing;
        root.innerHTML = `<div class="ticket-status"><span class="success-icon">${icon('mail', 24)}</span><h2>Your request</h2><p class="ticket-reference">Reference: ${esc(x.id)}</p><span class="status-badge is-active">${esc(x.status)}</span><p>Received ${fmtDate(x.created_at)}</p><div class="ticket-response"><h3>Response</h3><p>${esc(x.response || 'Your request is in the operator’s inbox. Any response will appear here. Keep this private link to check again.')}</p></div><a class="button button-outline" href="contact.html">Send another message ${icon('arrow-up-right', 15)}</a></div>`;
      } else if (S.receipt) {
        root.innerHTML = `<div class="ticket-status"><span class="success-icon">${icon('check', 25)}</span><h2>Message received.</h2><p>Your reference is <b>${esc(S.receipt.id)}</b>.</p><p>Save the private link below to check your request and read any reply. Keep it to yourself: anyone with this link can view the request status.</p><div class="share-link-box"><input readonly value="${esc(S.receiptURL)}" aria-label="Private request receipt" id="receipt-url"></div><a class="button button-primary wide" href="${esc(S.receiptURL)}">View my request ${icon('arrow-up-right', 17)}</a><p class="form-hint">This page is your receipt. We don’t automatically email a copy.</p></div>`;
        $('#receipt-url', root).addEventListener('focus', e => e.target.select());
      } else {
        const cr = S.ticketKind === 'copyright';
        root.innerHTML = `<form id="support-form"><h2>${isReport ? 'Submit a report' : 'Send us a message'}</h2><p class="form-subtitle">Every field marked * is required.</p>
${isReport ? `<div class="field"><label for="ticket-kind">Report type *</label><div class="select-wrap"><select id="ticket-kind"><option value="abuse" ${S.ticketKind === 'abuse' ? 'selected' : ''}>Abuse or prohibited content</option><option value="copyright" ${cr ? 'selected' : ''}>Copyright infringement</option></select>${icon('chevron-down', 16)}</div></div>` : ''}
<div class="form-grid"><div class="field"><label for="contact-name">Your name *</label><input id="contact-name" value="${esc(S.name)}" autocomplete="name" required maxlength="120"></div><div class="field"><label for="contact-email">Email address *</label><input id="contact-email" type="email" value="${esc(S.email)}" autocomplete="email" required maxlength="254"></div></div>
<div class="field"><label for="contact-subject">Subject *</label><input id="contact-subject" value="${esc(S.subject)}" required maxlength="180"></div>
<div class="field"><label for="contact-reference">Transfer link or code ${isReport ? '*' : '<span>Optional</span>'}</label><input id="contact-reference" value="${esc(S.reference)}" placeholder="Paste the link or 8-digit code" ${isReport ? 'required' : ''} maxlength="500"></div>
<div class="field"><label for="contact-message">${cr ? 'Describe the copyrighted work and the infringing material *' : 'How can we help? *'}</label><textarea id="contact-message" required minlength="20" maxlength="8000" rows="5" placeholder="${cr ? 'Identify the work you own or represent, the exact file involved, and where the original work can be found. Include enough detail for us to identify and assess your request.' : 'Please include enough detail for us to understand your request (at least 20 characters).'}">${esc(S.message)}</textarea></div>
${cr ? `<label class="check-label"><input type="checkbox" id="contact-decl" ${S.declarations ? 'checked' : ''} required><span>I have a good-faith belief that the disputed use is not authorized by the copyright owner, its agent, or the law. The information in this notice is accurate, and under penalty of perjury, I am the owner or authorized to act on the owner’s behalf. *</span></label><div class="field"><label for="copyright-signature">Electronic signature — full legal name *</label><input id="copyright-signature" value="${esc(S.signature)}" required maxlength="150"></div>` : ''}
${S.error ? alertBox(S.error) : ''}
<button class="button button-primary wide" ${S.busy ? 'disabled' : ''}>${S.busy ? spinner(17) : icon('send', 17)} ${S.busy ? 'Submitting…' : isReport ? 'Submit report' : 'Send message'}</button>
<p class="terms-note">Your details are handled according to our <a href="privacy.html">Privacy Policy</a>.</p></form>`;
        const map = { '#contact-name': 'name', '#contact-email': 'email', '#contact-subject': 'subject', '#contact-reference': 'reference', '#contact-message': 'message', '#copyright-signature': 'signature' };
        for (const [sel, key] of Object.entries(map)) { const el = $(sel, root); if (el) el.addEventListener('input', e => { S[key] = e.target.value; }); }
        const decl = $('#contact-decl', root); if (decl) decl.addEventListener('change', e => { S.declarations = e.target.checked; });
        const tk = $('#ticket-kind', root); if (tk) tk.addEventListener('change', e => { S.ticketKind = e.target.value; render(); });
        $('#support-form', root).addEventListener('submit', submit);
      }
    }

    const id = q.get('ticket'), secret = q.get('receipt');
    if (id && secret) {
      S.checking = true; render();
      request(`/api/tickets/${encodeURIComponent(id)}?receipt=${encodeURIComponent(secret)}`).then(d => { S.existing = d; }).catch(err => { S.error = errMsg(err); }).finally(() => { S.checking = false; render(); });
    } else render();
  }

  /* ------------------------------------------------------------------ ADMIN: operator inbox */
  const adminRoot = $('#admin-root');
  if (adminRoot) initAdmin(adminRoot);

  function initAdmin(root) {
    const S = { key: '', authenticated: false, tickets: [], busy: false, error: '', notice: '', selected: null, response: '', status: 'open', revoke: false };

    async function load() {
      S.busy = true; S.error = ''; render();
      try { S.tickets = (await request('/api/admin/tickets')).tickets; } catch (err) { S.error = errMsg(err); } finally { S.busy = false; render(); }
    }
    async function login(e) {
      e.preventDefault(); S.busy = true; S.error = ''; render();
      try { await request('/api/admin/login', { method: 'POST', body: jsonBody({ key: S.key }) }); S.authenticated = true; S.key = ''; S.busy = false; await load(); }
      catch (err) { S.error = errMsg(err); S.busy = false; render(); }
    }
    async function logout() {
      S.busy = true; S.error = ''; render();
      try { await request('/api/admin/logout', { method: 'POST' }); Object.assign(S, { authenticated: false, tickets: [], selected: null, response: '', key: '', notice: '' }); }
      catch (err) { S.error = errMsg(err); } finally { S.busy = false; render(); }
    }
    async function save(e) {
      e.preventDefault(); if (!S.selected) return;
      S.busy = true; S.error = ''; render();
      try {
        await request(`/api/admin/tickets/${encodeURIComponent(S.selected.id)}`, { method: 'PATCH', body: jsonBody({ response: S.response, status: S.status, revoke: S.revoke }) });
        S.notice = 'Request updated. The response is now available through its private receipt link.'; S.selected = null; S.busy = false; await load();
      } catch (err) { S.error = errMsg(err); S.busy = false; render(); }
    }

    function render() {
      if (!S.authenticated) {
        root.innerHTML = `<form id="admin-login" class="admin-login"><h2>Operator access</h2><p>Enter the private operator key to continue.</p><div class="field"><label for="admin-key">Operator key</label><input id="admin-key" type="password" autocomplete="current-password" required value="${esc(S.key)}"></div>${S.error ? alertBox(S.error) : ''}<button class="button button-primary" ${S.busy ? 'disabled' : ''}>${S.busy ? spinner(16) : icon('lock', 16)}Open inbox</button></form>`;
        $('#admin-key', root).addEventListener('input', e => { S.key = e.target.value; });
        $('#admin-login', root).addEventListener('submit', login);
        return;
      }
      const sel = S.selected;
      root.innerHTML = `${S.error ? alertBox(S.error) : ''}${S.notice ? alertBox(S.notice, true) : ''}
<div class="admin-top"><p>${S.tickets.length} requests</p><div class="button-row"><button class="button button-outline" type="button" id="adm-refresh" ${S.busy ? 'disabled' : ''}>${icon('history', 15)}Refresh</button><button class="button button-outline" type="button" id="adm-logout" ${S.busy ? 'disabled' : ''}>${icon('lock', 15)}Sign out</button></div></div>
<div class="admin-layout"><div class="admin-tickets">${!S.tickets.length ? `<div class="empty-state">${icon('mail', 27)}<h3>All quiet here.</h3><p>Submitted requests will appear in this inbox.</p></div>`
        : S.tickets.map(t => `<button class="admin-ticket ${sel && sel.id === t.id ? 'selected' : ''}" type="button" data-ticket="${esc(t.id)}"><span class="admin-ticket-top"><span class="mini-label">${esc(t.kind)}</span><span class="status-badge">${esc(t.status)}</span></span><h3>${esc(t.subject)}</h3><p>${esc(t.name)} · ${fmtDate(t.created_at)}</p></button>`).join('')}</div>
<div class="admin-detail">${sel ? `<form id="adm-form"><div class="eyebrow">${esc(sel.kind)} · ${esc(sel.id)}</div><h2>${esc(sel.subject)}</h2><p>${esc(sel.name)} · ${esc(sel.email)}</p>${sel.reference ? `<p class="admin-reference"><b>Transfer:</b> ${esc(sel.reference)}</p>` : ''}<div class="admin-message">${esc(sel.message)}</div>
${sel.kind === 'copyright' ? `<div class="copyright-declaration"><h3>Copyright declaration</h3><p>${sel.declarations ? 'Confirmed by the requester' : 'Not confirmed'}</p><p>Electronic signature: ${esc(sel.signature || 'Not supplied')}</p></div>` : ''}
<div class="field"><label for="ticket-status">Request status</label><select id="ticket-status">${['open', 'reviewing', 'resolved', 'dismissed'].map(s => `<option value="${s}" ${S.status === s ? 'selected' : ''}>${s[0].toUpperCase() + s.slice(1)}</option>`).join('')}</select></div>
<div class="field"><label for="operator-response">Response for the requester</label><textarea id="operator-response" rows="5" maxlength="4000">${esc(S.response)}</textarea></div>
${sel.transfer_id ? `<label class="check-label"><input type="checkbox" id="adm-revoke" ${S.revoke ? 'checked' : ''}><span>Revoke the referenced transfer when saving this response.</span></label>` : ''}
<button class="button button-primary" ${S.busy ? 'disabled' : ''}>${S.busy ? spinner(16) : icon('check', 16)}Save response</button></form>`
        : `<div class="empty-state">${icon('mail', 28)}<h3>A request needs a little attention?</h3><p>Select one to review and respond.</p></div>`}</div></div>`;
      $('#adm-refresh', root).addEventListener('click', load);
      $('#adm-logout', root).addEventListener('click', logout);
      bind(root, '[data-ticket]', 'click', e => {
        const t = S.tickets.find(x => x.id === e.currentTarget.dataset.ticket);
        Object.assign(S, { selected: t, response: t.response || '', status: t.status, revoke: false, notice: '' }); render();
      });
      const st = $('#ticket-status', root); if (st) st.addEventListener('change', e => { S.status = e.target.value; });
      const rs = $('#operator-response', root); if (rs) rs.addEventListener('input', e => { S.response = e.target.value; });
      const rv = $('#adm-revoke', root); if (rv) rv.addEventListener('change', e => { S.revoke = e.target.checked; });
      const form = $('#adm-form', root); if (form) form.addEventListener('submit', save);
    }

    render();
    request('/api/session').then(d => { if (d.admin) { S.authenticated = true; load(); } }).catch(() => undefined);
  }
})();
