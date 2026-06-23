// ── Sidebar mobile toggle ─────────────────────────────────────────────────────
const sidebarEl  = document.getElementById('sidebar');
const overlayEl  = document.getElementById('sidebar-overlay');
const toggleBtn  = document.getElementById('sidebar-toggle');

function openSidebar()  { sidebarEl.classList.add('open'); overlayEl.classList.add('visible'); }
function closeSidebar() { sidebarEl.classList.remove('open'); overlayEl.classList.remove('visible'); }
toggleBtn.addEventListener('click', openSidebar);
overlayEl.addEventListener('click', closeSidebar);

// Utility to parse and return a clean, user-friendly error message from tracebacks/API responses
function getCleanErrorMessage(errText) {
  if (!errText) return 'Unknown error occurred.';
  
  // Check for common API errors
  if (errText.includes('high demand') || errText.includes('experiencing high demand')) {
    return 'Google Gemini is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.';
  }
  if (errText.includes('503 UNAVAILABLE') || errText.includes('503 Service Unavailable')) {
    return 'Google Gemini API is temporarily unavailable (503). Please try again in a few minutes.';
  }
  if (errText.includes('Quota exceeded') || errText.includes('429')) {
    return 'API quota limit exceeded. Please wait a moment before trying again.';
  }
  if (errText.includes('GEMINI_API_KEY is required')) {
    return 'Gemini API Key is missing or invalid. Please configure it in Models & Keys.';
  }

  // Fallback: extract the last non-empty line of the error text
  const lines = errText.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  if (lines.length > 0) {
    const lastLine = lines[lines.length - 1];
    const match = lastLine.match(/^[a-zA-Z0-9.]+Exception:\s*(.+)$/) || 
                  lastLine.match(/^[a-zA-Z0-9.]+Error:\s*(.+)$/);
    if (match) {
      return match[1];
    }
    return lastLine;
  }
  return errText;
}

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
  if (name === 'upload') loadExistingUploads();
  if (name === 'models') loadModelConfiguration();
}

async function loadExistingUploads() {
  const list = document.getElementById('upload-file-list');
  if (list.children.length > 0) return; // already populated this session
  try {
    const res  = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    (data.pdfs || []).forEach(name => {
      const item = addFileItem(name, '', 'success', '✓ Uploaded');
      // hide the preview btn until status is known — it's already there
    });
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
  if (V2_INGEST_ENABLED) {
    fd.append('catalog_name', file.name.replace(/\.pdf$/i,'').replace(/[_-]+/g,' '));
    fd.append('source_type', 'catalog');
  }
  try {
    const endpoint = V2_INGEST_ENABLED ? '/admin-panel/api/v2/upload/' : '/admin-panel/api/upload/';
    const res  = await fetch(endpoint, { method: 'POST', body: fd });
    const data = await res.json();
    if (res.status === 409 || data.error) {
      setFileStatus(item, 'error', res.status === 409 ? '✗ Already exists' : '✗ ' + data.error);
      showToast(data.error, 'error');
    } else {
      setFileStatus(item, 'success', V2_INGEST_ENABLED ? '✓ Queued' : '✓ Uploaded');
      showToast(data.message || 'Uploaded successfully!', 'success');
      refreshStats();
      if (V2_INGEST_ENABLED && data.job_id) {
        pollV2IngestionJob(item, data.job_id);
      } else {
        document.getElementById('next-to-chunk').style.display = 'flex';
        loadPdfPreview(file.name);
      }
    }
  } catch(e) {
    setFileStatus(item, 'error', '✗ Failed');
    showToast('Upload failed.', 'error');
  }
}

async function pollV2IngestionJob(item, jobId) {
  for (;;) {
    await new Promise(resolve=>setTimeout(resolve,2000));
    try {
      const res=await fetch(`/admin-panel/api/v2/jobs/${jobId}/`);
      const data=await res.json();
      if (!res.ok || data.error) {
        setFileStatus(item,'error','✗ Status unavailable');
        return;
      }
      const progress=data.total_units?Math.round((data.completed_units/data.total_units)*100):0;
      setFileStatus(item,'pending',`Processing: ${data.job_stage} ${progress}%`);
      if (data.job_status==='succeeded') {
        const review=data.document_status==='review';
        setFileStatus(item,review?'pending':'success',review?'Review required':'✓ Extraction complete');
        showToast(review?'Extraction complete; review products in Django Admin.':'V2 extraction complete.',review?'info':'success');
        if (review) addV2ReviewButton(item,data.document_id);
        else addV2IndexButton(item,data.document_id);
        refreshStats();
        return;
      }
      if (data.job_status==='failed'||data.job_status==='cancelled') {
        setFileStatus(item,'error',`✗ ${data.job_status}`);
        showToast(data.error ? getCleanErrorMessage(data.error) : `V2 ingestion ${data.job_status}.`,'error', 10000);
        return;
      }
    } catch(error) {
      setFileStatus(item,'error','✗ Status check failed');
      return;
    }
  }
}

function addV2ReviewButton(item,documentId){
  const link=document.createElement('a');
  link.className='btn btn-sm btn-secondary';
  link.href=`/django-admin/catalog/productfamily/?document__id__exact=${encodeURIComponent(documentId)}`;
  link.target='_blank';
  link.rel='noopener';
  link.textContent='Review products';
  item.appendChild(link);
}

function addV2IndexButton(item,documentId){
  const button=document.createElement('button');
  button.type='button';
  button.className='btn btn-sm btn-secondary';
  button.textContent='Review indexing cost';
  button.addEventListener('click',async()=>{
    const auditResponse=await fetch(`/admin-panel/api/v2/documents/${documentId}/index-audit/`);
    const audit=await auditResponse.json();
    if(!auditResponse.ok||audit.error){showToast(audit.error||'Could not load indexing audit.','error');return;}
    if(audit.review_blocked){showToast(`${audit.review_blocked} chunks still require review.`,'error');return;}
    const count=audit.pending_embeddings;
    if(!window.confirm(`This will create ${count} OpenAI embedding(s). Continue?`)) return;
    button.disabled=true;
    button.textContent='Indexing…';
    try{
      const response=await fetch(`/admin-panel/api/v2/documents/${documentId}/index/`,{
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':CSRF},
        body:JSON.stringify({confirmed_embedding_count:count})
      });
      const data=await response.json();
      if(!response.ok||data.error) throw new Error(data.error||'Indexing failed.');
      setFileStatus(item,'success','✓ Ready');
      button.remove();
      showToast(`Indexed ${data.indexed} chunk(s).`,'success');
      refreshStats();
    }catch(error){
      showToast(error.message,'error');
      button.disabled=false;
      button.textContent='Retry indexing';
    }
  });
  item.appendChild(button);
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
    </button>
    <button class="btn btn-sm btn-danger file-delete-btn" onclick="deleteUploadedPdf('${escHtml(name)}', this)" title="Delete PDF">
      <i class="fa fa-trash"></i> Delete
    </button>`;
  document.getElementById('upload-file-list').prepend(div);
  return div;
}

function setFileStatus(item, cls, text) {
  const s = item.querySelector('.fstatus');
  s.className = `fstatus ${cls}`;
  s.textContent = text;
}

async function deleteUploadedPdf(filename, btn) {
  if (!confirm(`Delete "${filename}" and all its associated data?`)) return;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
  try {
    const res = await fetch('/admin-panel/api/delete-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename })
    });
    const data = await res.json();
    if (res.ok) {
      btn.closest('.file-item').remove();
      showToast(data.message || `"${filename}" deleted.`, 'success');
      refreshStats();
    } else {
      btn.disabled = false;
      btn.innerHTML = '<i class="fa fa-trash"></i> Delete';
      showToast(data.error || 'Delete failed.', 'error');
    }
  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-trash"></i> Delete';
    showToast('Delete failed.', 'error');
  }
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
  showSplitterForPdf(filename);
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
    if (data.pages.length) renderContinuousPreview(data.pages);

  } catch(e) {
    strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Failed to load preview.</div>';
  }
}

async function loadSplitPartPreview(idx) {
  const part = _splitParts[idx];
  if (!part || !_splitStem) return;
  const section = document.getElementById('pdf-preview-section');
  const strip   = document.getElementById('pdf-thumb-strip');
  const viewer  = document.getElementById('pdf-page-viewer');
  const nameEl  = document.getElementById('preview-pdf-name');
  const countEl = document.getElementById('preview-page-count');
  const subEl   = document.getElementById('preview-sub');

  section.style.display = 'block';
  nameEl.textContent    = part.filename;
  countEl.textContent   = `${part.page_count} page${part.page_count !== 1 ? 's' : ''}`;
  subEl.textContent     = `Split part · original pages ${part.pages}`;
  strip.innerHTML  = '<div class="pdf-preview-loading"><i class="fa fa-spinner fa-spin"></i> Rendering split part…</div>';
  viewer.innerHTML = '<div class="pdf-viewer-toolbar" style="justify-content:flex-start;color:var(--grey);font-size:12px;gap:6px"><i class="fa fa-hand-pointer"></i> Loading split preview</div><div class="pdf-viewer-scroll"><div class="pdf-preview-loading"><i class="fa fa-file-pdf"></i></div></div>';
  document.querySelectorAll('.split-part-item').forEach((el, i) => el.classList.toggle('active', i === idx));

  try {
    const url = `/admin-panel/api/pdf-preview/?stem=${encodeURIComponent(_splitStem)}&part=${encodeURIComponent(part.filename)}`;
    const res  = await fetch(url);
    const data = await res.json();
    if (data.error) {
      strip.innerHTML = `<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> ${data.error}</div>`;
      return;
    }
    countEl.textContent = `${data.total} page${data.total !== 1 ? 's' : ''}`;
    strip.innerHTML = data.pages.map((p, i) => `
      <div class="pdf-thumb-item${i === 0 ? ' active' : ''}" onclick="selectPreviewPage(${i})" id="thumb-${i}">
        <img src="${p.thumb}" alt="Page ${p.num}" loading="lazy"/>
        <span class="thumb-num">${p.num}</span>
      </div>`).join('');
    window._previewPages = data.pages;
    if (data.pages.length) renderContinuousPreview(data.pages);
  } catch(e) {
    strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Failed to load split preview.</div>';
  }
}

let _zoomLevel = 100;
let _previewScrollFrame = null;

function renderContinuousPreview(pages) {
  _zoomLevel = 100;
  const viewer = document.getElementById('pdf-page-viewer');
  viewer.innerHTML = `
    <div class="pdf-viewer-toolbar">
      <button class="zoom-btn" onclick="adjustZoom(-25)" title="Zoom out"><i class="fa fa-minus"></i></button>
      <span class="zoom-level" id="zoom-level-label">100%</span>
      <button class="zoom-btn" onclick="adjustZoom(25)" title="Zoom in"><i class="fa fa-plus"></i></button>
      <button class="zoom-btn" onclick="adjustZoom(0)" title="Reset zoom"><i class="fa fa-compress-arrows-alt"></i></button>
      <span class="scroll-hint"><i class="fa fa-mouse"></i> Scroll continuously</span>
      <span class="page-label" id="preview-current-page">Page ${pages[0].num} of ${pages.length}</span>
    </div>
    <div class="pdf-viewer-scroll" id="viewer-scroll">
      <div class="pdf-page-stack" id="pdf-page-stack">
        ${pages.map((page, idx) => `
          <figure class="pdf-page-sheet" id="preview-page-${idx}" data-page-index="${idx}">
            <img src="${page.full}" alt="Page ${page.num}" loading="${idx < 2 ? 'eager' : 'lazy'}"/>
            <figcaption>Page ${page.num}</figcaption>
          </figure>`).join('')}
      </div>
    </div>`;

  const scroll = document.getElementById('viewer-scroll');
  scroll.addEventListener('scroll', () => {
    if (_previewScrollFrame) return;
    _previewScrollFrame = requestAnimationFrame(() => {
      _previewScrollFrame = null;
      syncPreviewPageFromScroll();
    });
  }, { passive: true });
  setActivePreviewPage(0);
}

function selectPreviewPage(idx) {
  const pages  = window._previewPages || [];
  if (!pages[idx]) return;
  const scroll = document.getElementById('viewer-scroll');
  const page   = document.getElementById('preview-page-' + idx);
  const first  = document.getElementById('preview-page-0');
  if (!scroll || !page || !first) return;
  scroll.scrollTo({ top: page.offsetTop - first.offsetTop, behavior: 'smooth' });
  setActivePreviewPage(idx);
}

function syncPreviewPageFromScroll() {
  const scroll = document.getElementById('viewer-scroll');
  const sheets = [...document.querySelectorAll('.pdf-page-sheet')];
  if (!scroll || !sheets.length) return;

  const firstOffset = sheets[0].offsetTop;
  const marker = scroll.scrollTop + Math.min(120, scroll.clientHeight * 0.25);
  let activeIdx = 0;
  sheets.forEach((sheet, idx) => {
    if (sheet.offsetTop - firstOffset <= marker) activeIdx = idx;
  });
  setActivePreviewPage(activeIdx);
}

function setActivePreviewPage(idx) {
  const pages = window._previewPages || [];
  if (!pages[idx]) return;
  document.querySelectorAll('.pdf-thumb-item').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
  const label = document.getElementById('preview-current-page');
  if (label) label.textContent = `Page ${pages[idx].num} of ${pages.length}`;

  const strip = document.getElementById('pdf-thumb-strip');
  const thumb = document.getElementById('thumb-' + idx);
  if (strip && thumb) {
    if (thumb.offsetTop < strip.scrollTop) {
      strip.scrollTop = thumb.offsetTop;
    } else if (thumb.offsetTop + thumb.offsetHeight > strip.scrollTop + strip.clientHeight) {
      strip.scrollTop = thumb.offsetTop + thumb.offsetHeight - strip.clientHeight;
    }
  }
}

function adjustZoom(delta) {
  const stack = document.getElementById('pdf-page-stack');
  const label = document.getElementById('zoom-level-label');
  if (!stack) return;
  if (delta === 0) { _zoomLevel = 100; }
  else { _zoomLevel = Math.min(300, Math.max(25, _zoomLevel + delta)); }
  stack.style.width = _zoomLevel + '%';
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
function getParsingInstructions() {
  return (document.getElementById('chunk-parsing-instructions')?.value
    || document.getElementById('split-parsing-instructions')?.value
    || '').trim();
}

function syncParsingInstructions(source) {
  const value = source.value;
  ['chunk-parsing-instructions', 'split-parsing-instructions'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el !== source) el.value = value;
  });
}

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
      body: JSON.stringify({
        filename: selectedPdf,
        parsing_instructions: getParsingInstructions()
      })
    });
    const data = await res.json();
    clearInterval(ticker);
    fill.style.width = '100%'; pct.textContent = '100%';
    label.textContent = 'Complete';

    if (data.error) {
      log.textContent += '\n✗ ERROR:\n' + data.error;
      showToast(`Chunking failed: ${getCleanErrorMessage(data.error)}`, 'error', 10000);
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

function markStepDone(name) {
  const s = document.querySelector(`.step-item[data-step="${name}"]`);
  if (s) { s.classList.add('done'); s.querySelector('.step-circle').innerHTML = '<i class="fa fa-check"></i>'; }
}

// ── Models & encrypted API keys ──────────────────────────────────────────────
let _modelOptions = { vision_models: [], chat_models: { gemini: [], openai: [] } };
let _savedChatModel = '';

function fillModelSelect(selectId, options, selectedValue) {
  const select = document.getElementById(selectId);
  if (!select) return;
  select.innerHTML = (options || []).map(option =>
    `<option value="${escHtml(option.value)}"${option.value === selectedValue ? ' selected' : ''}>${escHtml(option.label)}</option>`
  ).join('');
}

function setKeyStatus(provider, keyInfo) {
  const status = document.getElementById(`${provider}-key-status`);
  const input = document.getElementById(`${provider}-api-key`);
  if (!status || !input) return;
  status.className = `key-status ${keyInfo.configured ? 'configured' : 'missing'}`;
  status.innerHTML = keyInfo.configured
    ? `<i class="fa fa-check-circle"></i> Configured ${escHtml(keyInfo.masked)}`
    : '<i class="fa fa-exclamation-circle"></i> Not configured';
  input.value = '';
  input.placeholder = keyInfo.configured ? 'Leave blank to keep current key' : `Enter ${provider === 'openai' ? 'an OpenAI' : 'a Gemini'} key`;
}

async function loadModelConfiguration(force = false) {
  if (!IS_ADMIN) return;
  const message = document.getElementById('model-config-message');
  if (message) message.textContent = 'Loading secure model configuration…';
  try {
    const res = await fetch('/admin-panel/api/model-config/');
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not load model configuration.');

    _modelOptions = data.options;
    _savedChatModel = data.configuration.chat_model;
    document.getElementById('embedding-model').value = data.configuration.embedding_model;
    fillModelSelect('vision-model', data.options.vision_models, data.configuration.vision_model);
    document.getElementById('chat-provider').value = data.configuration.chat_provider;
    refreshChatModelOptions(data.configuration.chat_model);
    setKeyStatus('openai', data.keys.openai);
    setKeyStatus('gemini', data.keys.gemini);
    document.getElementById('clear-openai-key').checked = false;
    document.getElementById('clear-gemini-key').checked = false;
    if (message) message.textContent = 'Configuration loaded. Blank key fields keep the saved encrypted keys.';
  } catch (error) {
    if (message) message.textContent = error.message;
    showToast(error.message, 'error');
  }
}

function refreshChatModelOptions(selectedValue = '') {
  const provider = document.getElementById('chat-provider')?.value || 'gemini';
  const options = _modelOptions.chat_models?.[provider] || [];
  const candidate = selectedValue || (options.some(option => option.value === _savedChatModel) ? _savedChatModel : options[0]?.value);
  fillModelSelect('chat-model', options, candidate);
  refreshChatRoutingSummary();
}

function refreshChatRoutingSummary() {
  const provider = document.getElementById('chat-provider')?.value || 'gemini';
  const options = _modelOptions.chat_models?.[provider] || [];
  const selected = options.find(option => option.value === document.getElementById('chat-model')?.value);
  const summary = document.getElementById('routing-summary');
  const providerLabel = provider === 'openai' ? 'OpenAI' : provider === 'groq' ? 'Groq' : 'Google Gemini';
  if (summary) {
    summary.innerHTML = `<i class="fa fa-route"></i> Product chat will use <strong>${providerLabel}</strong>${selected ? ` · ${escHtml(selected.label)}` : ''}.`;
  }
}

function toggleSecretVisibility(inputId, button) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
  button.innerHTML = `<i class="fa fa-${input.type === 'password' ? 'eye' : 'eye-slash'}"></i>`;
}

async function saveModelConfiguration() {
  const button = document.getElementById('btn-save-model-config');
  const message = document.getElementById('model-config-message');
  const payload = {
    openai_api_key: document.getElementById('openai-api-key').value.trim(),
    gemini_api_key: document.getElementById('gemini-api-key').value.trim(),
    clear_openai_key: document.getElementById('clear-openai-key').checked,
    clear_gemini_key: document.getElementById('clear-gemini-key').checked,
    vision_model: document.getElementById('vision-model').value,
    chat_provider: document.getElementById('chat-provider').value,
    chat_model: document.getElementById('chat-model').value,
  };

  button.disabled = true;
  button.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Saving securely…';
  if (message) message.textContent = 'Encrypting and saving configuration…';
  try {
    const res = await fetch('/admin-panel/api/model-config/save/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not save model configuration.');
    _modelOptions = data.options;
    _savedChatModel = data.configuration.chat_model;
    setKeyStatus('openai', data.keys.openai);
    setKeyStatus('gemini', data.keys.gemini);
    document.getElementById('clear-openai-key').checked = false;
    document.getElementById('clear-gemini-key').checked = false;
    if (message) message.textContent = data.message;
    showToast(data.message, 'success');
  } catch (error) {
    if (message) message.textContent = error.message;
    showToast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.innerHTML = '<i class="fa fa-save"></i> Save Models &amp; Keys';
  }
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
    refreshStats();
    loadPdfList();
    document.querySelectorAll('.catalog-table tbody tr').forEach(row => {
      if (row.querySelector('.pname')?.textContent === filename) row.remove();
    });
  } catch(e) { showToast('Delete failed.', 'error'); }
}

// -- Stats refresh -------------------------------------------------------------────────────
async function refreshStats() {
  try {
    const res  = await fetch('/admin-panel/api/stats/');
    const data = await res.json();
    document.getElementById('stat-pdfs').textContent      = data.total_pdfs ?? '—';
    document.getElementById('stat-processed').textContent = (data.processed||[]).length;
    document.getElementById('stat-indexed').textContent   = data.indexed ?? '—';
    document.getElementById('stat-chunks').textContent    = data.total_chunks ?? data.indexed ?? '—';
    updateWorkflowProgress(data);
  } catch(e) {}
}

function _num(value) {
  const parsed = parseInt(String(value ?? '0').replace(/[^0-9]/g, ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function updateWorkflowProgress(data = {}) {
  const root = document.getElementById('workflow-progress');
  if (!root) return;

  const totalPdfs = 'session_uploaded_pdfs' in data ? data.session_uploaded_pdfs.length : -1;
  const chunks     = 'session_chunks' in data ? _num(data.session_chunks) : -1;
  const indexed    = 'session_indexed' in data ? _num(data.session_indexed) : -1;

  // If session keys missing (initial DOM call fallback) force 0
  const sessionPdfs    = totalPdfs < 0 ? 0 : totalPdfs;
  const sessionChunks  = chunks < 0 ? 0 : chunks;
  const sessionIndexed = indexed < 0 ? 0 : indexed;
  const allPdfs    = _num(data.total_pdfs ?? document.getElementById('stat-pdfs')?.textContent);
  const allChunks  = _num(data.indexed ?? document.getElementById('stat-chunks')?.textContent);
  const allIndexed = _num(data.indexed ?? document.getElementById('stat-indexed')?.textContent);

  let percent = 0;
  let stepNo = 0;
  let active = 'upload';
  let title = 'Catalog setup progress';
  let sub = 'Upload a PDF to start the catalog pipeline.';

  if (sessionPdfs > 0) {
    percent = 25; stepNo = 1; active = 'chunk';
    title = 'PDF uploaded';
    sub = 'Next step: create product chunks from the uploaded catalog.';
  }
  if (sessionChunks > 0) {
    percent = 75; stepNo = 3; active = 'index';
    title = 'Chunks created';
    sub = 'Product chunks are ready. Index them for semantic search.';
  }
  if (sessionIndexed > 0) {
    percent = 100; stepNo = 4; active = 'chat';
    title = 'Catalog search ready';
    sub = 'Chunks are indexed. You can validate answers in Test Chat.';
  }

  document.getElementById('workflow-step-label').textContent = `Step ${stepNo} of 4`;
  document.getElementById('workflow-progress-title').textContent = title;
  document.getElementById('workflow-progress-sub').textContent = sub;
  document.getElementById('workflow-progress-percent').textContent = `${percent}%`;
  document.getElementById('workflow-progress-fill').style.width = `${percent}%`;

  document.getElementById('progress-upload-count').textContent = `${allPdfs} uploaded`;
  document.getElementById('progress-chunk-count').textContent = `${allChunks} chunk${allChunks === 1 ? '' : 's'}`;
  document.getElementById('progress-index-count').textContent = `${allIndexed} indexed`;

  const done = {
    upload: sessionPdfs > 0,
    chunk: sessionChunks > 0,
    index: sessionIndexed > 0,
    chat: sessionIndexed > 0,
  };
  document.querySelectorAll('[data-progress-step]').forEach(item => {
    const key = item.dataset.progressStep;
    item.classList.toggle('done', Boolean(done[key]));
    item.classList.toggle('active', key === active && !done[key]);
  });
}

// ── Text Splitter ────────────────────────────────────
let _splitMode = 'uniform'; // 'uniform' | 'custom'
let _customRangeCount = 0;
let _totalPages = 0;
let _splitterOpen = true;

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
  row.className = 'custom-range-row';
  row.innerHTML = `
    <span class="custom-range-part">Part ${idx}</span>
    <input type="number" id="cr-start-${idx}" min="1" value="" placeholder="From"
      class="custom-range-input"
      oninput="_validateCustomRanges()"/>
    <span class="custom-range-to">to</span>
    <input type="number" id="cr-end-${idx}" min="1" value="" placeholder="To"
      class="custom-range-input"
      oninput="_validateCustomRanges()"/>
    <button class="custom-range-remove" onclick="removeCustomRange(${idx})" title="Remove">
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

  const splitterSection = document.getElementById('splitter-section');
  splitterSection.style.display = 'block';
  splitterSection.classList.remove('collapsed');
  _splitterOpen = true;
  _splitMode = 'uniform';
  _customRangeCount = 0;
  _totalPages = 0;
  document.getElementById('split-mode-uniform').classList.add('active');
  document.getElementById('split-mode-custom').classList.remove('active');
  document.getElementById('uniform-split-controls').style.display = 'block';
  document.getElementById('custom-split-controls').style.display  = 'none';
  document.querySelectorAll('.splitter-presets .preset-btn').forEach((button, idx) => {
    button.classList.toggle('active', idx === 0);
  });
  document.getElementById('pages-per-input').value = 5;
  _pagesPerPart = 5;
  document.getElementById('custom-ranges-list').innerHTML = '';
  document.getElementById('custom-split-preview').className = 'splitter-preview';
  document.getElementById('splitter-body').style.display = 'block';
  const toggleButton = document.getElementById('btn-toggle-splitter');
  toggleButton.innerHTML = '<i class="fa fa-chevron-up"></i>';
  toggleButton.title = 'Collapse splitter';
  document.getElementById('split-parts-wrap').style.display = 'none';
  document.getElementById('split-parts-list').innerHTML = '';
  const wholeBtn = document.getElementById('btn-process-whole-pdf');
  if (wholeBtn) wholeBtn.style.display = '';
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
  document.getElementById('splitter-section').classList.toggle('collapsed', !_splitterOpen);
  document.getElementById('splitter-body').style.display    = _splitterOpen ? 'block' : 'none';
  const toggleButton = document.getElementById('btn-toggle-splitter');
  toggleButton.innerHTML = `<i class="fa fa-chevron-${_splitterOpen?'up':'down'}"></i>`;
  toggleButton.title = _splitterOpen ? 'Collapse splitter' : 'Expand splitter';
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
  document.querySelectorAll('.splitter-presets .preset-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pages-per-input').value = val;
  _pagesPerPart = val;
  const badge = document.getElementById('splitter-pages-badge').textContent;
  const total  = parseInt(badge);
  if (!isNaN(total)) _updateSplitterPreview(total);
}

document.addEventListener('DOMContentLoaded', () => {
  // On fresh page load always start progress from session (0 until user does something)
  updateWorkflowProgress({ session_uploaded_pdfs: [], session_chunks: 0, session_indexed: 0 });
  refreshStats();
  const customInput = document.getElementById('pages-per-input');
  if (customInput) {
    customInput.addEventListener('input', () => {
      const v = parseInt(customInput.value);
      if (v > 0) {
        _pagesPerPart = v;
        document.querySelectorAll('.splitter-presets .preset-btn').forEach(b => b.classList.remove('active'));
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
  document.getElementById('next-to-chunk').style.display = 'none';
  const wholeBtn = document.getElementById('btn-process-whole-pdf');
  if (wholeBtn) wholeBtn.style.display = 'none';
  list.innerHTML = parts.map((p, i) => `
    <div class="split-part-item" id="split-part-${i}" onclick="loadSplitPartPreview(${i})" title="Preview this split part">
      <div class="split-part-num">${i+1}</div>
      <div class="split-part-info">
        <div class="part-name">${escHtml(p.filename)}</div>
        <div class="part-pages">Pages ${escHtml(p.pages)} &nbsp;·&nbsp; ${p.page_count} page${p.page_count!==1?'s':''}</div>
      </div>
      <span class="split-part-status pending" id="split-status-${i}">Pending</span>
      <button class="btn btn-sm btn-secondary split-preview-btn" onclick="event.stopPropagation();loadSplitPartPreview(${i})">
        <i class="fa fa-eye"></i>
      </button>
      <button class="btn btn-sm btn-secondary" id="split-btn-${i}" onclick="event.stopPropagation();runSingleSplit(${i})">
        <i class="fa fa-play"></i> Run
      </button>
    </div>`).join('');
  if (parts.length) loadSplitPartPreview(0);
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
      body: JSON.stringify({
        stem: _splitStem,
        part_file: part.filename,
        parsing_instructions: getParsingInstructions()
      })
    });
    const data = await res.json();
    if (data.error) {
      itemEl.className = 'split-part-item errored';
      statEl.className = 'split-part-status errored';
      statEl.textContent = '✗ Failed';
      showToast(`Part ${idx+1} failed: ${getCleanErrorMessage(data.error)}`, 'error', 10000);
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
    showToast(`Network error on part ${idx+1}.`, 'error', 10000);
  } finally {
    const currentBtn = document.getElementById(`split-btn-${idx}`);
    if (currentBtn) {
      if (currentBtn.innerHTML.includes('check')) {
        currentBtn.disabled = true;
      } else {
        currentBtn.innerHTML = '<i class="fa fa-play"></i> Run';
        currentBtn.disabled = false;
      }
    }
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

  // Build tabs
  tabs.innerHTML = chunks.map((c, i) =>
    `<button class="chunk-tab${i===0?' active':''}" onclick="switchChunk(${i})">${escHtml(c.filename.replace(/\.md$/, ''))}</button>`
  ).join('');

  // Build content panes
  body.innerHTML = chunks.map((c, i) => `
    <div class="chunk-content${i===0?' visible':''}" id="chunk-pane-${i}">
      <div class="chunk-toolbar">
        <span>${escHtml(c.filename)}</span>
        <div>
          <button class="btn btn-sm btn-secondary" id="chunk-edit-${i}" onclick="editChunk(${i})"><i class="fa fa-edit"></i> Edit</button>
          <button class="btn btn-sm btn-primary" id="chunk-save-${i}" onclick="saveChunk(${i})" style="display:none"><i class="fa fa-save"></i> Save</button>
          <button class="btn btn-sm btn-secondary" id="chunk-cancel-${i}" onclick="cancelEditChunk(${i})" style="display:none">Cancel</button>
        </div>
      </div>
      <div class="chunk-rendered" id="chunk-rendered-${i}">${mdToHtml(c.content)}</div>
      <textarea class="chunk-editor" id="chunk-editor-${i}" style="display:none">${escHtml(c.content)}</textarea>
    </div>`).join('');
}

function editChunk(idx) {
  document.getElementById(`chunk-rendered-${idx}`).style.display = 'none';
  document.getElementById(`chunk-editor-${idx}`).style.display = 'block';
  document.getElementById(`chunk-edit-${idx}`).style.display = 'none';
  document.getElementById(`chunk-save-${idx}`).style.display = 'inline-flex';
  document.getElementById(`chunk-cancel-${idx}`).style.display = 'inline-flex';
}

function cancelEditChunk(idx) {
  const pane = document.getElementById(`chunk-pane-${idx}`);
  const pdfName = document.getElementById('chunks-drawer-title').textContent;
  const chunk = _chunksCache[pdfName]?.[idx];
  pane.querySelector(`#chunk-editor-${idx}`).value = chunk?.content || '';
  document.getElementById(`chunk-rendered-${idx}`).style.display = 'block';
  document.getElementById(`chunk-editor-${idx}`).style.display = 'none';
  document.getElementById(`chunk-edit-${idx}`).style.display = 'inline-flex';
  document.getElementById(`chunk-save-${idx}`).style.display = 'none';
  document.getElementById(`chunk-cancel-${idx}`).style.display = 'none';
}

async function saveChunk(idx) {
  const pdfName = document.getElementById('chunks-drawer-title').textContent;
  const chunk = _chunksCache[pdfName]?.[idx];
  if (!chunk) return;
  const content = document.getElementById(`chunk-editor-${idx}`).value;
  const btn = document.getElementById(`chunk-save-${idx}`);
  btn.disabled = true;
  try {
    const res = await fetch('/admin-panel/api/chunks/save/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ pdf: pdfName, filename: chunk.filename, content })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Save failed.');
    chunk.content = content;
    document.getElementById(`chunk-rendered-${idx}`).innerHTML = mdToHtml(content);
    cancelEditChunk(idx);
    showToast('Chunk saved. Re-index after review.', 'success');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    btn.disabled = false;
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
