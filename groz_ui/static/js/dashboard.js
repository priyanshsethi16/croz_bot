// ── Sidebar mobile toggle ─────────────────────────────────────────────────────
const sidebarEl  = document.getElementById('sidebar');
const overlayEl  = document.getElementById('sidebar-overlay');
const toggleBtn  = document.getElementById('sidebar-toggle');

function openSidebar()  { sidebarEl.classList.add('open'); overlayEl.classList.add('visible'); }
function closeSidebar() { sidebarEl.classList.remove('open'); overlayEl.classList.remove('visible'); }
toggleBtn.addEventListener('click', openSidebar);
overlayEl.addEventListener('click', closeSidebar);

// ── Panel navigation ──────────────────────────────────────────────────────────
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
  document.querySelectorAll('.step-item').forEach(s => s.classList.remove('active'));

  document.getElementById('panel-' + name).classList.add('active');
  document.querySelector(`.sidebar-link[data-panel="${name}"]`)?.classList.add('active');
  document.querySelector(`.step-item[data-step="${name}"]`)?.classList.add('active');

  closeSidebar();
  if (name === 'chunk') loadPdfList();
  if (name === 'index') loadIndexList();
  if (name === 'upload') loadExistingUploads();
}

async function loadExistingUploads() {
  const list = document.getElementById('upload-file-list');
  try {
    const res  = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    list.innerHTML = '';
    (data.pdfs || []).forEach(name => addFileItem(name, '', 'success', '✓ Uploaded'));
  } catch(e) {}
}

// Open from ?panel= query param
(function() {
  const p = new URLSearchParams(location.search).get('panel');
  if (p && document.getElementById('panel-' + p)) showPanel(p);
})();

// ── Upload ────────────────────────────────────────────────────────────────────
const zone = document.getElementById('upload-zone');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  [...e.dataTransfer.files].forEach(f => uploadFile(f));
});
document.getElementById('pdf-file-input').addEventListener('change', e => {
  [...e.target.files].forEach(f => uploadFile(f));
  e.target.value = '';
});

async function uploadFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    showToast('Only PDF files are allowed.', 'error'); return;
  }
  const item = addFileItem(file.name, formatBytes(file.size), 'pending', 'Uploading…');
  // Check duplicate before uploading
  const fd = new FormData();
  fd.append('pdf', file);
  fd.append('csrfmiddlewaretoken', CSRF);
  try {
    const res  = await fetch('/admin-panel/api/upload/', { method: 'POST', body: fd });
    const data = await res.json();
    if (res.status === 409 || data.error) {
      setFileStatus(item, 'error', res.status === 409 ? '✗ Already exists' : '✗ ' + data.error);
      showToast(data.error, 'error');
    } else {
      setFileStatus(item, 'success', '✓ Uploaded');
      showToast(data.message || 'Uploaded successfully!', 'success');
      refreshStats();
      document.getElementById('next-to-chunk').style.display = 'flex';
      showSplitterForPdf(file.name);
      loadPdfPreview(file.name);
    }
  } catch(e) {
    setFileStatus(item, 'error', '✗ Failed');
    showToast('Upload failed.', 'error');
  }
}

function addFileItem(name, size, statusClass, statusText) {
  const div = document.createElement('div');
  div.className = 'file-item';
  div.innerHTML = `
    <div class="file-item-icon"><i class="fa fa-file-pdf"></i></div>
    <div class="file-item-info">
      <div class="fname">${escHtml(name)}</div>
      <div class="fsize">${size}</div>
    </div>
    <span class="fstatus ${statusClass}">${statusText}</span>
    <button class="btn btn-sm btn-secondary file-preview-btn" onclick="loadPdfPreview('${escHtml(name)}')" title="Preview pages">
      <i class="fa fa-eye"></i> Preview
    </button>`;
  document.getElementById('upload-file-list').prepend(div);
  return div;
}

function setFileStatus(item, cls, text) {
  const s = item.querySelector('.fstatus');
  s.className = `fstatus ${cls}`;
  s.textContent = text;
}

function formatBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

// ── PDF Page Preview ─────────────────────────────────────────────────────────────
async function loadPdfPreview(filename) {
  const section = document.getElementById('pdf-preview-section');
  const strip   = document.getElementById('pdf-thumb-strip');
  const viewer  = document.getElementById('pdf-page-viewer');
  const nameEl  = document.getElementById('preview-pdf-name');
  const countEl = document.getElementById('preview-page-count');
  const subEl   = document.getElementById('preview-sub');

  section.style.display = 'block';
  nameEl.textContent    = filename;
  countEl.textContent   = '';
  subEl.textContent     = 'Loading pages…';
  strip.innerHTML  = '<div class="pdf-preview-loading"><i class="fa fa-spinner fa-spin"></i> Rendering pages…</div>';
  viewer.innerHTML = '<div class="pdf-viewer-toolbar" style="justify-content:flex-start;color:var(--grey);font-size:12px;gap:6px"><i class="fa fa-hand-pointer"></i> Select a page to preview</div><div class="pdf-viewer-scroll"><div class="pdf-preview-loading"><i class="fa fa-file-pdf"></i></div></div>';

  try {
    const res  = await fetch('/admin-panel/api/pdf-preview/?pdf=' + encodeURIComponent(filename));
    const data = await res.json();
    if (data.error) {
      strip.innerHTML = `<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> ${data.error}</div>`;
      return;
    }
    countEl.textContent = data.total + ' pages';
    subEl.textContent   = data.pages.length < data.total
      ? `Showing first ${data.pages.length} of ${data.total} pages`
      : `${data.total} page${data.total !== 1 ? 's' : ''}`;

    strip.innerHTML = data.pages.map((p, i) => `
      <div class="pdf-thumb-item${i === 0 ? ' active' : ''}" onclick="selectPreviewPage(${i})" id="thumb-${i}">
        <img src="${p.thumb}" alt="Page ${p.num}" loading="lazy"/>
        <span class="thumb-num">${p.num}</span>
      </div>`).join('');

    window._previewPages = data.pages;
    if (data.pages.length) selectPreviewPage(0);

  } catch(e) {
    strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Failed to load preview.</div>';
  }
}

let _zoomLevel = 100;

function selectPreviewPage(idx) {
  const pages  = window._previewPages || [];
  if (!pages[idx]) return;
  _zoomLevel = 100;
  const viewer = document.getElementById('pdf-page-viewer');
  viewer.innerHTML = `
    <div class="pdf-viewer-toolbar">
      <button class="zoom-btn" onclick="adjustZoom(-25)" title="Zoom out"><i class="fa fa-minus"></i></button>
      <span class="zoom-level" id="zoom-level-label">100%</span>
      <button class="zoom-btn" onclick="adjustZoom(25)" title="Zoom in"><i class="fa fa-plus"></i></button>
      <button class="zoom-btn" onclick="adjustZoom(0)" title="Reset zoom"><i class="fa fa-compress-arrows-alt"></i></button>
      <span class="page-label">Page ${pages[idx].num} of ${pages.length}</span>
    </div>
    <div class="pdf-viewer-scroll" id="viewer-scroll">
      <img src="${pages[idx].full}" id="viewer-img" alt="Page ${pages[idx].num}" style="width:100%;max-width:100%;height:auto"/>
    </div>`;
  document.querySelectorAll('.pdf-thumb-item').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
  document.getElementById('thumb-' + idx)?.scrollIntoView({ block: 'nearest' });
}

function adjustZoom(delta) {
  const img   = document.getElementById('viewer-img');
  const label = document.getElementById('zoom-level-label');
  if (!img) return;
  if (delta === 0) { _zoomLevel = 100; }
  else { _zoomLevel = Math.min(300, Math.max(25, _zoomLevel + delta)); }
  img.style.width    = _zoomLevel + '%';
  img.style.maxWidth = 'none';
  label.textContent  = _zoomLevel + '%';
}

// ── Load PDF list for chunk panel ─────────────────────────────────────────────
async function loadPdfList() {
  try {
    const res  = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    const grid    = document.getElementById('pdf-selector-grid');
    const chunked = new Set(data.chunked || []);
    grid.innerHTML = '';
    (data.pdfs || []).forEach(name => {
      const stem    = name.replace(/\.pdf$/i, '');
      const isDone  = chunked.has(stem);
      const card    = document.createElement('div');
      card.className = 'pdf-select-card' + (isDone ? ' chunked-done' : '');
      card.dataset.pdf = name;
      card.innerHTML = `
        <i class="fa fa-file-pdf"></i>
        <div>
          <div class="pdf-name">${escHtml(name)}</div>
          <div class="pdf-sub">${isDone
            ? '<i class="fa fa-check-circle" style="color:#22c55e"></i> Already chunked'
            : 'Click to select'}</div>
        </div>
        ${isDone ? '<span class="pdf-done-badge"><i class="fa fa-check"></i></span>' : ''}`;
      if (!isDone) card.addEventListener('click', () => selectPdf(card, name));
      else card.title = 'Chunks already exist for this PDF';
      grid.appendChild(card);
    });
    if (!data.pdfs?.length) {
      grid.innerHTML = '<p style="color:var(--grey);font-size:13px;padding:10px 0">No PDFs uploaded yet. <button class="btn btn-sm btn-primary" onclick="showPanel(\'upload\')">Upload one →</button></p>';
    }
  } catch(e) {}
}

let selectedPdf = null;
function selectPdf(card, name) {
  document.querySelectorAll('.pdf-select-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  selectedPdf = name;
  document.getElementById('btn-run-chunk').disabled = false;
}

// ── Create Chunks (Pipeline) ──────────────────────────────────────────────────
async function runChunking() {
  if (!selectedPdf) { showToast('Please select a PDF first.', 'error'); return; }

  const btn   = document.getElementById('btn-run-chunk');
  const prog  = document.getElementById('chunk-progress');
  const fill  = document.getElementById('chunk-fill');
  const pct   = document.getElementById('chunk-pct');
  const log   = document.getElementById('chunk-log');
  const label = document.getElementById('chunk-progress-label');

  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';
  prog.classList.add('visible');
  log.classList.add('visible');
  log.textContent = `▶ Starting chunk extraction for: ${selectedPdf}\n`;

  let p = 0;
  const ticker = setInterval(() => {
    p = Math.min(p + 2, 88);
    fill.style.width = p + '%';
    pct.textContent  = p + '%';
  }, 600);

  try {
    const res  = await fetch('/admin-panel/api/pipeline/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: selectedPdf })
    });
    const data = await res.json();
    clearInterval(ticker);
    fill.style.width = '100%'; pct.textContent = '100%';
    label.textContent = 'Complete';

    if (data.error) {
      log.textContent += '\n✗ ERROR:\n' + data.error;
      showToast('Chunking failed.', 'error');
    } else {
      log.textContent += data.output || '\n✓ Chunks created successfully.';
      showToast('Product chunks created!', 'success');
      refreshStats();
      document.getElementById('next-to-index').style.display = 'flex';
      markStepDone('chunk');
    }
  } catch(e) {
    clearInterval(ticker);
    log.textContent += '\n✗ Network error.';
    showToast('Request failed.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-layer-group"></i> Create Chunks';
  }
}

// ── Index & Embed ─────────────────────────────────────────────────────────────
let indexMode = 'incremental';
function setIndexMode(mode) {
  indexMode = mode;
  document.querySelectorAll('.index-option-card').forEach(c => c.classList.remove('selected'));
  document.querySelector(`.index-option-card[data-mode="${mode}"]`).classList.add('selected');
}

async function loadIndexList() {
  try {
    const [pdfRes, statsRes] = await Promise.all([
      fetch('/admin-panel/api/pdfs/'),
      fetch('/admin-panel/api/stats/'),
    ]);
    const pdfData   = await pdfRes.json();
    const statsData = await statsRes.json();

    // Build set of PDF stems that have chunks
    const chunked = new Set(pdfData.chunked || []);
    // Build set of PDF stems that have indexed docs in chroma
    const indexedCount = statsData.indexed || 0;
    // We mark a PDF as "indexed" if it has chunks (chroma count > 0 means something is indexed)
    // Use processed list to show per-PDF status
    const processed = statsData.processed || [];
    const processedMap = {};
    processed.forEach(p => { processedMap[p.name] = p; });

    let container = document.getElementById('index-pdf-list');
    if (!container) {
      // Insert before the index-options div
      const optionsEl = document.querySelector('#panel-index .index-options');
      if (optionsEl) {
        container = document.createElement('div');
        container.id = 'index-pdf-list';
        container.style.cssText = 'margin-bottom:16px;display:flex;flex-direction:column;gap:8px;';
        optionsEl.parentNode.insertBefore(container, optionsEl);
      }
    }
    if (!container) return;

    const pdfs = pdfData.pdfs || [];
    if (!pdfs.length) {
      container.innerHTML = '<p style="color:var(--grey);font-size:13px">No PDFs found.</p>';
      return;
    }

    container.innerHTML = pdfs.map(name => {
      const stem      = name.replace(/\.pdf$/i, '');
      const hasChunks = chunked.has(stem);
      const info      = processedMap[stem];
      const isIndexed = hasChunks && indexedCount > 0;
      const badge = isIndexed
        ? '<span class="badge badge-green"><i class="fa fa-check-circle"></i> Indexed</span>'
        : hasChunks
          ? '<span class="badge badge-orange"><i class="fa fa-clock"></i> Not indexed</span>'
          : '<span class="badge badge-grey"><i class="fa fa-minus"></i> No chunks</span>';
      const chunksInfo = info ? `${info.chunks} chunks` : '';
      return `
        <div style="display:flex;align-items:center;gap:12px;padding:11px 14px;
             border:1px solid var(--border);border-radius:8px;background:#fafafa">
          <i class="fa fa-file-pdf" style="color:var(--orange);font-size:16px;flex-shrink:0"></i>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(name)}</div>
            ${chunksInfo ? `<div style="font-size:11px;color:var(--grey);margin-top:2px">${chunksInfo}</div>` : ''}
          </div>
          ${badge}
        </div>`;
    }).join('');
  } catch(e) {}
}

async function runIndexing() {
  const reset = indexMode === 'reset';
  const btn   = document.getElementById('btn-run-index');
  const prog  = document.getElementById('index-progress');
  const fill  = document.getElementById('index-fill');
  const pct   = document.getElementById('index-pct');
  const log   = document.getElementById('index-log');
  const label = document.getElementById('index-progress-label');

  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Indexing…';
  prog.classList.add('visible');
  log.classList.add('visible');
  log.textContent = reset ? '▶ Resetting and re-indexing all chunks…\n' : '▶ Running incremental indexing…\n';

  let p = 0;
  const ticker = setInterval(() => {
    p = Math.min(p + 4, 88);
    fill.style.width = p + '%';
    pct.textContent  = p + '%';
  }, 400);

  try {
    const res  = await fetch('/admin-panel/api/ingest/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ reset })
    });
    const data = await res.json();
    clearInterval(ticker);
    fill.style.width = '100%'; pct.textContent = '100%';
    label.textContent = 'Complete';

    if (data.error) {
      log.textContent += '\n✗ ERROR:\n' + data.error;
      showToast('Indexing failed.', 'error');
    } else {
      log.textContent += data.output || '\n✓ Indexing complete.';
      showToast('Indexing complete!', 'success');
      refreshStats();
      markStepDone('index');
      loadIndexList();
    }
  } catch(e) {
    clearInterval(ticker);
    log.textContent += '\n✗ Network error.';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-bolt"></i> Run Indexing';
  }
}

function markStepDone(name) {
  const s = document.querySelector(`.step-item[data-step="${name}"]`);
  if (s) { s.classList.add('done'); s.querySelector('.step-circle').innerHTML = '<i class="fa fa-check"></i>'; }
}

// ── Admin Chat ────────────────────────────────────────────────────────────────
async function adminChat() {
  const input = document.getElementById('admin-chat-input');
  const msgs  = document.getElementById('admin-msgs');
  const query = input.value.trim();
  if (!query) return;

  appendAdminMsg('user', escHtml(query));
  input.value = '';

  const typing = document.createElement('div');
  typing.id = 'admin-typing';
  typing.className = 'admin-msg bot';
  typing.innerHTML = `<div class="admin-msg-avatar">GZ</div>
    <div class="admin-msg-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
  msgs.appendChild(typing); msgs.scrollTop = msgs.scrollHeight;

  try {
    const res  = await fetch('/admin-panel/api/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ query })
    });
    const data = await res.json();
    document.getElementById('admin-typing')?.remove();
    if (data.error) appendAdminMsg('bot', `<span style="color:var(--orange)">${data.error}</span>`);
    else {
      const src = (data.sources||[]).map(s=>`<span style="background:var(--orange-light);color:var(--orange);padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700">${escHtml(s.name||s.code)}</span>`).join(' ');
      appendAdminMsg('bot', renderMarkdown(data.answer) + (src ? `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">${src}</div>` : ''));
    }
  } catch(e) {
    document.getElementById('admin-typing')?.remove();
    appendAdminMsg('bot', 'Network error.');
  }
}

function adminAskCat(cat, el) {
  document.querySelectorAll('.admin-cat-item').forEach(i => i.classList.remove('active'));
  el.classList.add('active');
  const input = document.getElementById('admin-chat-input');
  input.value = cat === 'all'
    ? 'Show all available products'
    : `Show ${cat} products and their specifications`;
  adminChat();
}

function appendAdminMsg(role, html) {
  const msgs = document.getElementById('admin-msgs');
  const div  = document.createElement('div');
  div.className = `admin-msg ${role}`;
  div.innerHTML = `<div class="admin-msg-avatar">${role==='bot'?'GZ':'<i class="fa fa-user"></i>'}</div>
    <div class="admin-msg-bubble msg-content">${html}</div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
}

// -- Delete PDF ------------------------------------------------------------------
async function deletePdf(filename) {
  if (!confirm(`Delete "${filename}" and ALL associated chunks, products and index data?\nThis cannot be undone.`)) return;
  try {
    const res  = await fetch('/admin-panel/api/delete-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast(data.message, 'success');

    const stem = filename.replace(/\.pdf$/i, '');

    // 1. Remove ALL matching rows from catalog table (stem or full filename)
    document.querySelectorAll('.catalog-table tbody tr').forEach(row => {
      const pname = row.querySelector('.pname')?.textContent?.trim();
      if (pname === stem || pname === filename) row.remove();
    });

    // 2. Remove from upload list
    document.querySelectorAll('#upload-file-list .file-item').forEach(item => {
      const fname = item.querySelector('.fname')?.textContent?.trim();
      if (fname === filename || fname === stem) item.remove();
    });

    // 3. Remove from chunk panel grid
    document.querySelectorAll('#pdf-selector-grid .pdf-select-card').forEach(card => {
      if (card.dataset.pdf === filename || card.dataset.pdf === stem) card.remove();
    });

    // 4. Invalidate chunks cache
    delete _chunksCache[stem];
    delete _chunksCache[filename];

    // 5. Refresh stats counters + reload both live lists
    refreshStats();
    loadPdfList();
    loadExistingUploads();
  } catch(e) { showToast('Delete failed.', 'error'); }
}

// -- Stats refresh + table rebuild -------------------------------------------
async function refreshStats() {
  try {
    const res  = await fetch('/admin-panel/api/stats/');
    const data = await res.json();
    document.getElementById('stat-pdfs').textContent      = data.total_pdfs ?? '—';
    document.getElementById('stat-processed').textContent = (data.processed||[]).length;
    document.getElementById('stat-indexed').textContent   = data.indexed ?? '—';
    document.getElementById('stat-chunks').textContent    = data.total_chunks ?? '—';
    _rebuildCatalogTable(data);
  } catch(e) {}
}

function _rebuildCatalogTable(data) {
  const processed   = data.processed   || [];
  const unprocessed = data.unprocessed || [];
  const allEmpty    = processed.length === 0 && unprocessed.length === 0;

  // If nothing at all, show empty state and hide table
  const tableWrap = document.querySelector('#panel-overview .table-wrap');
  const emptyEl   = document.getElementById('catalog-empty');

  if (allEmpty) {
    if (tableWrap) tableWrap.style.display = 'none';
    if (emptyEl)   emptyEl.style.display   = 'block';
    return;
  }

  // We have rows — ensure table is visible, empty state hidden
  if (emptyEl)   emptyEl.style.display   = 'none';
  if (tableWrap) tableWrap.style.display = '';

  const tbody = document.getElementById('catalog-tbody');
  if (!tbody) { location.reload(); return; }

  var rows = [];

  processed.forEach(function(item) {
    var adminBtns = IS_ADMIN
      ? '<button class="btn btn-sm btn-danger" onclick="deletePdf(\'' + escHtml(item.name) + '.pdf\')"><i class="fa fa-trash"></i> Delete</button>'
      : '';
    rows.push('<tr>'
      + '<td><div class="pdf-name-cell"><i class="fa fa-file-pdf"></i><span class="pname">' + escHtml(item.name) + '</span></div></td>'
      + '<td><span class="badge badge-blue">' + item.chunks + '</span></td>'
      + '<td><span class="badge badge-green">' + item.products + '</span></td>'
      + '<td><span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span></td>'
      + '<td><div class="table-actions">'
      + '<button class="btn btn-sm btn-secondary" onclick="openChunks(\'' + escHtml(item.name) + '\')"><i class="fa fa-layer-group"></i> Chunks</button>'
      + '<button class="btn btn-sm btn-secondary" onclick="showPanel(\'chat\')"><i class="fa fa-comments"></i> Test</button>'
      + adminBtns
      + '</div></td></tr>');
  });

  if (IS_ADMIN) {
    unprocessed.forEach(function(pdf_name) {
      rows.push('<tr>'
        + '<td><div class="pdf-name-cell"><i class="fa fa-file-pdf"></i><span class="pname">' + escHtml(pdf_name) + '</span></div></td>'
        + '<td><span class="badge badge-grey">&mdash;</span></td>'
        + '<td><span class="badge badge-grey">&mdash;</span></td>'
        + '<td><span class="badge badge-grey"><i class="fa fa-clock"></i> Not processed</span></td>'
        + '<td><div class="table-actions">'
        + '<button class="btn btn-sm btn-danger" onclick="deletePdf(\'' + escHtml(pdf_name) + '\')"><i class="fa fa-trash"></i> Delete</button>'
        + '</div></td></tr>');
    });
  }

  tbody.innerHTML = rows.join('');
}

// ── Text Splitter ────────────────────────────────────
let _splitMode = 'uniform'; // 'uniform' | 'custom'
let _customRangeCount = 0;
let _totalPages = 0;

function setSplitMode(mode) {
  _splitMode = mode;
  document.getElementById('split-mode-uniform').classList.toggle('active', mode === 'uniform');
  document.getElementById('split-mode-custom').classList.toggle('active', mode === 'custom');
  document.getElementById('uniform-split-controls').style.display = mode === 'uniform' ? 'block' : 'none';
  document.getElementById('custom-split-controls').style.display  = mode === 'custom'  ? 'block' : 'none';
  if (mode === 'custom' && _customRangeCount === 0) addCustomRange();
}

function addCustomRange() {
  _customRangeCount++;
  const idx  = _customRangeCount;
  const list = document.getElementById('custom-ranges-list');
  const row  = document.createElement('div');
  row.id = `cr-row-${idx}`;
  row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 12px;background:#fafafa;border:1.5px solid var(--border);border-radius:8px';
  row.innerHTML = `
    <span style="font-size:12px;font-weight:700;color:var(--grey);min-width:52px">Part ${idx}</span>
    <span style="font-size:12px;color:var(--grey)">Pages</span>
    <input type="number" id="cr-start-${idx}" min="1" value="" placeholder="From"
      style="width:70px;padding:6px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;outline:none"
      oninput="_validateCustomRanges()"/>
    <span style="font-size:12px;color:var(--grey)">to</span>
    <input type="number" id="cr-end-${idx}" min="1" value="" placeholder="To"
      style="width:70px;padding:6px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;font-family:inherit;outline:none"
      oninput="_validateCustomRanges()"/>
    <button onclick="removeCustomRange(${idx})" style="margin-left:auto;background:none;border:none;color:#ccc;cursor:pointer;font-size:14px;padding:2px 6px" title="Remove">
      <i class="fa fa-times"></i>
    </button>`;
  list.appendChild(row);
  _validateCustomRanges();
}

function removeCustomRange(idx) {
  document.getElementById(`cr-row-${idx}`)?.remove();
  _validateCustomRanges();
}

function _getCustomRanges() {
  const rows = document.querySelectorAll('#custom-ranges-list > div');
  return Array.from(rows).map(row => {
    const s = parseInt(row.querySelector('input:first-of-type').value);
    const e = parseInt(row.querySelector('input:last-of-type').value);
    return { start: s, end: e };
  }).filter(r => !isNaN(r.start) && !isNaN(r.end));
}

function _validateCustomRanges() {
  const ranges  = _getCustomRanges();
  const preview = document.getElementById('custom-split-preview');
  if (!ranges.length) { preview.className = 'splitter-preview'; return; }
  const total = ranges.reduce((s, r) => s + (r.end - r.start + 1), 0);
  const errors = ranges.filter(r => r.start < 1 || r.end < r.start || (_totalPages > 0 && r.end > _totalPages));
  preview.className = 'splitter-preview visible';
  if (errors.length) {
    preview.innerHTML = `<i class="fa fa-exclamation-triangle" style="color:#dc2626"></i> &nbsp; Invalid ranges detected`;
  } else {
    preview.innerHTML = `<i class="fa fa-check-circle" style="color:#22c55e"></i> &nbsp;
      <strong>${ranges.length}</strong> parts &nbsp;·&nbsp; <strong>${total}</strong> pages total`;
  }
}

function doSplit() {
  if (_splitMode === 'custom') splitPdfCustom();
  else splitPdf();
}

async function splitPdfCustom() {
  if (!selectedPdf) { showToast('Select a PDF first.', 'error'); return; }
  const ranges = _getCustomRanges();
  if (!ranges.length) { showToast('Add at least one page range.', 'error'); return; }
  const btn = document.getElementById('btn-split-pdf');
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Splitting…';
  try {
    const res  = await fetch('/admin-panel/api/split-pdf-custom/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: selectedPdf, ranges })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    _splitStem  = data.stem;
    _splitParts = data.splits;
    showToast(data.message, 'success');
    renderSplitParts(data.splits);
  } catch(e) { showToast('Split failed: ' + e.message, 'error'); console.error(e); }
  finally { btn.disabled = false; btn.innerHTML = '<i class="fa fa-cut"></i> Split &amp; Process Parts'; }
}

function showSplitterForPdf(filename) {
  selectedPdf = filename;
  _splitStem  = filename.replace(/\.pdf$/i, '');
  _splitParts = [];

  document.getElementById('splitter-section').style.display = 'block';
  _splitterOpen = true;
  _splitMode = 'uniform';
  _customRangeCount = 0;
  _totalPages = 0;
  document.getElementById('split-mode-uniform').classList.add('active');
  document.getElementById('split-mode-custom').classList.remove('active');
  document.getElementById('uniform-split-controls').style.display = 'block';
  document.getElementById('custom-split-controls').style.display  = 'none';
  document.getElementById('custom-ranges-list').innerHTML = '';
  document.getElementById('custom-split-preview').className = 'splitter-preview';
  document.getElementById('splitter-body').style.display = 'block';
  document.getElementById('btn-toggle-splitter').innerHTML =
    '<i class="fa fa-chevron-up"></i> Collapse';
  document.getElementById('split-parts-wrap').style.display = 'none';
  document.getElementById('split-parts-list').innerHTML = '';
  document.getElementById('splitter-preview').className = 'splitter-preview';
  document.getElementById('splitter-preview').innerHTML = '';
  document.getElementById('splitter-pdf-name').textContent = filename;
  document.getElementById('splitter-pages-badge').innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
  document.getElementById('splitter-info-bar').style.display = 'flex';
  _loadPageCount(filename);
}


let _splitStem    = null;
let _splitParts   = [];
let _pagesPerPart = 5;

function toggleSplitter() {
  _splitterOpen = !_splitterOpen;
  document.getElementById('splitter-body').style.display    = _splitterOpen ? 'block' : 'none';
  document.getElementById('btn-toggle-splitter').innerHTML  =
    `<i class="fa fa-chevron-${_splitterOpen?'up':'down'}" id="splitter-chevron"></i> ${_splitterOpen?'Collapse':'Expand'}`;
  if (_splitterOpen && selectedPdf) _loadPageCount(selectedPdf);
}

async function _loadPageCount(pdf) {
  const bar    = document.getElementById('splitter-info-bar');
  const nameEl = document.getElementById('splitter-pdf-name');
  const pagesEl= document.getElementById('splitter-pages-badge');
  const hint    = document.getElementById('split-hint');
  const btnSplit = document.getElementById('btn-split-pdf');

  bar.style.display = 'flex';
  nameEl.textContent  = pdf;
  pagesEl.textContent = 'Loading…';

  try {
    const res  = await fetch(`/admin-panel/api/pdf-pages/?pdf=${encodeURIComponent(pdf)}`);
    const data = await res.json();
    if (data.error) { pagesEl.textContent = 'Error'; return; }
    pagesEl.textContent = `${data.pages} pages`;
    _totalPages = data.pages;
    if (hint) hint.textContent = '';
    if (btnSplit) btnSplit.disabled = false;
    _updateSplitterPreview(data.pages);
  } catch(e) { pagesEl.textContent = 'Error'; }
}

function setPreset(btn, val) {
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pages-per-input').value = val;
  _pagesPerPart = val;
  const badge = document.getElementById('splitter-pages-badge').textContent;
  const total  = parseInt(badge);
  if (!isNaN(total)) _updateSplitterPreview(total);
}

document.addEventListener('DOMContentLoaded', () => {
  const customInput = document.getElementById('pages-per-input');
  if (customInput) {
    customInput.addEventListener('input', () => {
      const v = parseInt(customInput.value);
      if (v > 0) {
        _pagesPerPart = v;
        document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
        const badge = document.getElementById('splitter-pages-badge').textContent;
        const total  = parseInt(badge);
        if (!isNaN(total)) _updateSplitterPreview(total);
      }
    });
  }
});

function _updateSplitterPreview(totalPages) {
  const pp      = parseInt(document.getElementById('pages-per-input').value) || _pagesPerPart;
  const parts   = Math.ceil(totalPages / pp);
  const preview = document.getElementById('splitter-preview');
  preview.className = 'splitter-preview visible';
  preview.innerHTML =
    `<i class="fa fa-info-circle" style="color:var(--orange)"></i> &nbsp;
     <strong>${totalPages}</strong> pages ÷ <strong>${pp}</strong> pages/part
     = <strong style="color:var(--orange)">${parts} part${parts!==1?'s':''}</strong>
     &nbsp;·&nbsp; Each processed sequentially`;
}

async function splitPdf() {
  if (!selectedPdf) { showToast('Select a PDF first.', 'error'); return; }
  const pp  = parseInt(document.getElementById('pages-per-input').value) || _pagesPerPart;
  const btn = document.getElementById('btn-split-pdf');

  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Splitting…';

  try {
    const res  = await fetch('/admin-panel/api/split-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: selectedPdf, pages_per: pp })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    _splitStem  = data.stem;
    _splitParts = data.splits;
    showToast(data.message, 'success');
    renderSplitParts(data.splits);
  } catch(e) { showToast('Split failed.', 'error'); }
  finally { btn.disabled = false; btn.innerHTML = '<i class="fa fa-cut"></i> Split PDF'; }
}

function renderSplitParts(parts) {
  const wrap  = document.getElementById('split-parts-wrap');
  const list  = document.getElementById('split-parts-list');
  const title = document.getElementById('split-parts-title');

  wrap.style.display = 'block';
  title.textContent  = `${parts.length} parts ready to process`;
  list.innerHTML = parts.map((p, i) => `
    <div class="split-part-item" id="split-part-${i}">
      <div class="split-part-num">${i+1}</div>
      <div class="split-part-info">
        <div class="part-name">${escHtml(p.filename)}</div>
        <div class="part-pages">Pages ${escHtml(p.pages)} &nbsp;·&nbsp; ${p.page_count} page${p.page_count!==1?'s':''}</div>
      </div>
      <span class="split-part-status pending" id="split-status-${i}">Pending</span>
      <button class="btn btn-sm btn-secondary" id="split-btn-${i}" onclick="runSingleSplit(${i})">
        <i class="fa fa-play"></i> Run
      </button>
    </div>`).join('');
}

async function runSingleSplit(idx) {
  const part   = _splitParts[idx];
  const itemEl = document.getElementById(`split-part-${idx}`);
  const statEl = document.getElementById(`split-status-${idx}`);
  const btnEl  = document.getElementById(`split-btn-${idx}`);

  itemEl.className = 'split-part-item running';
  statEl.className = 'split-part-status running';
  statEl.textContent = 'Running…';
  btnEl.disabled = true;
  btnEl.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';

  try {
    const res  = await fetch('/admin-panel/api/pipeline-split/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ stem: _splitStem, part_file: part.filename })
    });
    const data = await res.json();
    if (data.error) {
      itemEl.className = 'split-part-item errored';
      statEl.className = 'split-part-status errored';
      statEl.textContent = '✗ Failed';
      showToast(`Part ${idx+1} failed.`, 'error');
    } else {
      itemEl.className = 'split-part-item done';
      statEl.className = 'split-part-status done';
      statEl.textContent = '✓ Done';
      btnEl.innerHTML = '<i class="fa fa-check"></i>';
      refreshStats();
    }
  } catch(e) {
    itemEl.className = 'split-part-item errored';
    statEl.className = 'split-part-status errored';
    statEl.textContent = '✗ Error';
  } finally {
    if (!document.getElementById(`split-btn-${idx}`).innerHTML.includes('check'))
      btnEl.disabled = false;
  }
}

async function runAllSplits() {
  const btn = document.getElementById('btn-run-all-splits');
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';
  for (let i = 0; i < _splitParts.length; i++) {
    const statEl = document.getElementById(`split-status-${i}`);
    if (statEl && statEl.textContent === '✓ Done') continue; // skip already done
    await runSingleSplit(i);
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fa fa-check"></i> All Done';
  document.getElementById('next-to-index').style.display = 'flex';
  showToast('All parts processed!', 'success');
}

// ── Chunks Drawer ─────────────────────────────────────────────────────
let _chunksCache = {};

async function openChunks(pdfName) {
  const drawer  = document.getElementById('chunks-drawer');
  const overlay = document.getElementById('chunks-overlay');
  const title   = document.getElementById('chunks-drawer-title');
  const sub     = document.getElementById('chunks-drawer-sub');
  const tabs    = document.getElementById('chunks-tabs');
  const body    = document.getElementById('chunks-body');

  title.textContent = pdfName;
  sub.textContent   = 'Product markdown chunks';
  tabs.innerHTML    = '';
  body.innerHTML    = '<div class="chunks-loading"><i class="fa fa-spinner fa-spin"></i> Loading chunks…</div>';

  drawer.classList.add('open');
  overlay.classList.add('visible');
  document.body.style.overflow = 'hidden';

  if (_chunksCache[pdfName]) {
    renderChunks(pdfName, _chunksCache[pdfName]);
    return;
  }

  try {
    const res  = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(pdfName)}`);
    const data = await res.json();
    if (data.error) { body.innerHTML = `<p style="color:var(--orange)">${data.error}</p>`; return; }
    _chunksCache[pdfName] = data.chunks;
    renderChunks(pdfName, data.chunks);
  } catch(e) {
    body.innerHTML = '<p style="color:var(--orange)"><i class="fa fa-exclamation-triangle"></i> Failed to load chunks.</p>';
  }
}

function renderChunks(pdfName, chunks) {
  const tabs = document.getElementById('chunks-tabs');
  const body = document.getElementById('chunks-body');
  const sub  = document.getElementById('chunks-drawer-sub');

  sub.textContent = `${chunks.length} chunk${chunks.length !== 1 ? 's' : ''} found`;

  if (!chunks.length) {
    body.innerHTML = '<div class="chunks-loading">No chunks found for this PDF.</div>';
    return;
  }

  // Store for edit access
  _currentChunks  = chunks;
  _currentPdfName = pdfName;

  // Build tabs
  tabs.innerHTML = chunks.map((c, i) =>
    `<button class="chunk-tab${i===0?' active':''}" onclick="switchChunk(${i})">${escHtml(c.filename.replace(/\.md$/, ''))}</button>`
  ).join('');

  // Build content panes — each has view + edit mode
  body.innerHTML = chunks.map((c, i) => `
    <div class="chunk-content${i===0?' visible':''}" id="chunk-pane-${i}">
      <div class="chunk-toolbar">
        <button class="btn btn-sm btn-secondary chunk-edit-btn" id="edit-btn-${i}" onclick="startEditChunk(${i})">
          <i class="fa fa-pen"></i> Edit
        </button>
        <button class="btn btn-sm btn-primary chunk-save-btn" id="save-btn-${i}" onclick="saveChunk(${i})" style="display:none">
          <i class="fa fa-save"></i> Save
        </button>
        <button class="btn btn-sm btn-secondary chunk-cancel-btn" id="cancel-btn-${i}" onclick="cancelEditChunk(${i})" style="display:none">
          <i class="fa fa-times"></i> Cancel
        </button>
        <span class="chunk-save-status" id="save-status-${i}"></span>
      </div>
      <div id="chunk-view-${i}">${mdToHtml(c.content)}</div>
      <textarea id="chunk-edit-${i}" class="chunk-editor" style="display:none">${escHtml(c.content)}</textarea>
    </div>
  `).join('');
}

let _currentChunks  = [];
let _currentPdfName = '';

function startEditChunk(i) {
  const viewEl = document.getElementById(`chunk-view-${i}`);
  // Make the rendered view directly editable
  viewEl.contentEditable = 'true';
  viewEl.classList.add('chunk-editable');
  viewEl.focus();
  document.getElementById(`chunk-edit-${i}`).style.display = 'none'; // keep hidden
  document.getElementById(`edit-btn-${i}`).style.display   = 'none';
  document.getElementById(`save-btn-${i}`).style.display   = 'inline-flex';
  document.getElementById(`cancel-btn-${i}`).style.display = 'inline-flex';
}

function cancelEditChunk(i) {
  const viewEl = document.getElementById(`chunk-view-${i}`);
  viewEl.contentEditable = 'false';
  viewEl.classList.remove('chunk-editable');
  // Restore original rendered content
  viewEl.innerHTML = mdToHtml(_currentChunks[i].content);
  document.getElementById(`edit-btn-${i}`).style.display   = 'inline-flex';
  document.getElementById(`save-btn-${i}`).style.display   = 'none';
  document.getElementById(`cancel-btn-${i}`).style.display = 'none';
  document.getElementById(`save-status-${i}`).textContent  = '';
}

function _htmlToMd(html) {
  // Convert edited HTML back to clean markdown
  return html
    .replace(/<h1[^>]*>(.*?)<\/h1>/gi,  (_, t) => `# ${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<h2[^>]*>(.*?)<\/h2>/gi,  (_, t) => `## ${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<h3[^>]*>(.*?)<\/h3>/gi,  (_, t) => `### ${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<h4[^>]*>(.*?)<\/h4>/gi,  (_, t) => `#### ${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<strong>(.*?)<\/strong>/gi,(_, t) => `**${t}**`)
    .replace(/<code>(.*?)<\/code>/gi,    (_, t) => `\`${t}\``)
    .replace(/<li>(.*?)<\/li>/gi,        (_, t) => `- ${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<ul>|<\/ul>|<ol>|<\/ol>/gi, '')
    .replace(/<p>(.*?)<\/p>/gi,          (_, t) => `${t.replace(/<[^>]+>/g,'')}\n`)
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<hr\s*\/?>/gi, '---\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

async function saveChunk(i) {
  const chunk    = _currentChunks[i];
  const viewEl   = document.getElementById(`chunk-view-${i}`);
  const newMd    = _htmlToMd(viewEl.innerHTML);
  const statusEl = document.getElementById(`save-status-${i}`);
  const saveBtn  = document.getElementById(`save-btn-${i}`);

  saveBtn.disabled = true;
  statusEl.textContent = 'Saving…';
  statusEl.style.color = 'var(--grey)';

  try {
    const res  = await fetch('/admin-panel/api/chunk-save/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ part: chunk.part, filename: chunk.filename, content: newMd }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Save failed.');

    // Update cache with new markdown
    _currentChunks[i].content = newMd;
    delete _chunksCache[_currentPdfName];

    // Exit edit mode
    cancelEditChunk(i);
    statusEl.textContent = '✓ Saved — indexing started…';
    statusEl.style.color = '#16a34a';

    // Auto run incremental indexing in background
    _autoReindex(statusEl);
  } catch (e) {
    statusEl.textContent = '✗ ' + e.message;
    statusEl.style.color = '#dc2626';
  } finally {
    saveBtn.disabled = false;
  }
}

async function _autoReindex(statusEl) {
  try {
    const res  = await fetch('/admin-panel/api/ingest/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ reset: false }),
    });
    const data = await res.json();
    if (res.ok) {
      if (statusEl) { statusEl.textContent = '✓ Saved & indexed successfully'; statusEl.style.color = '#16a34a'; }
      showToast('Chunk saved & re-indexed!', 'success');
      refreshStats();
      loadIndexList();
    } else {
      if (statusEl) { statusEl.textContent = '✓ Saved — indexing failed: ' + (data.error || ''); statusEl.style.color = '#dc2626'; }
      showToast('Saved but indexing failed.', 'error');
    }
  } catch(e) {
    if (statusEl) { statusEl.textContent = '✓ Saved — indexing error: ' + e.message; statusEl.style.color = '#dc2626'; }
  }
}

function switchChunk(idx) {
  document.querySelectorAll('.chunk-tab').forEach((t, i) => t.classList.toggle('active', i === idx));
  document.querySelectorAll('.chunk-content').forEach((p, i) => p.classList.toggle('visible', i === idx));
  document.getElementById('chunks-body').scrollTop = 0;
}

function closeChunks() {
  document.getElementById('chunks-drawer').classList.remove('open');
  document.getElementById('chunks-overlay').classList.remove('visible');
  document.body.style.overflow = '';
}

// Minimal markdown → HTML for chunk display
function mdToHtml(md) {
  if (!md) return '';
  let h = escHtml(md);

  // Tables
  h = h.replace(/\|(.+)\|\n\|[-| :]+\|\n((?:\|.+\|\n?)*)/g, (_, header, rows) => {
    const ths = header.split('|').filter(s => s.trim()).map(s => `<th>${s.trim()}</th>`).join('');
    const trs = rows.trim().split('\n').map(row => {
      const tds = row.split('|').filter(s => s.trim()).map(s => `<td>${s.trim()}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  });

  // Headings
  h = h.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
  h = h.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
  h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');

  // Inline
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/`([^`]+)`/g,     '<code>$1</code>');
  h = h.replace(/^---$/gm,        '<hr>');

  // Lists
  h = h.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  h = h.replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`);

  // Paragraphs (lines not already wrapped)
  h = h.replace(/^(?!<[hultHULT]).+$/gm, line => line.trim() ? `<p>${line}</p>` : '');

  return h;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── API Keys Panel ─────────────────────────────────────────────────────────────

function toggleKeyVis(name) {
  const inp = document.getElementById(`key-${name}`);
  if (!inp) return;
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

async function loadApiKeys() {
  try {
    const res  = await fetch('/admin-panel/api/api-keys/', { headers: { 'X-CSRFToken': CSRF } });
    const data = await res.json();
    if (data.keys) {
      for (const [name, val] of Object.entries(data.keys)) {
        const inp = document.getElementById(`key-${name}`);
        if (inp) inp.value = val;
      }
      _showApiMsg('Current keys loaded (masked). Enter new value to update.', 'info');
    }
  } catch (e) {
    _showApiMsg('Failed to load keys: ' + e.message, 'error');
  }
}

async function saveApiKeys() {
  const keyNames = ['GROQ_API_KEY', 'GEMINI_API_KEY_1', 'GEMINI_API_KEY_2', 'GEMINI_API_KEY_3'];
  const body = {};
  keyNames.forEach(n => {
    const inp = document.getElementById(`key-${n}`);
    if (inp) body[n] = inp.value.trim();
  });
  try {
    const res  = await fetch('/admin-panel/api/api-keys/save/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.ok) {
      _showApiMsg(`✓ ${data.message}`, 'success');
    } else {
      _showApiMsg(data.error || 'Save failed.', 'error');
    }
  } catch (e) {
    _showApiMsg('Network error: ' + e.message, 'error');
  }
}

function _showApiMsg(msg, type) {
  const el = document.getElementById('apikeys-msg');
  if (!el) return;
  const colors = { success: '#16a34a', error: '#dc2626', info: '#2563eb' };
  el.style.display = 'block';
  el.style.color = colors[type] || '#666';
  el.textContent = msg;
}

// Auto-load keys when API Keys panel is opened
const _origShowPanel = typeof showPanel === 'function' ? showPanel : null;
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-panel="apikeys"]').forEach(btn => {
    btn.addEventListener('click', () => { setTimeout(loadApiKeys, 100); });
  });
});
