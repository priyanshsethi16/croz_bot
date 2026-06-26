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
  if (name === 'overview') { _userSelectedPdf = null; refreshStats(); }
  if (name === 'chunk') loadPdfList();
  if (name === 'families') loadFamilyPanel();
  if (name === 'upload') loadExistingUploads();
  if (name === 'models') loadModelConfiguration();
  if (name === 'index') loadIndexPanel();
}

async function loadSplitPartPreviewByName(partFilename, parentStem) {
  const section = document.getElementById('pdf-preview-section');
  const strip   = document.getElementById('pdf-thumb-strip');
  const viewer  = document.getElementById('pdf-page-viewer');
  const countEl = document.getElementById('preview-page-count');
  const subEl   = document.getElementById('preview-sub');

  section.style.display = 'block';
  strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-spinner fa-spin"></i> Rendering pages…</div>';
  viewer.innerHTML = '<div class="pdf-viewer-toolbar" style="justify-content:flex-start;color:var(--grey);font-size:12px;gap:6px"><i class="fa fa-hand-pointer"></i> Select a page to preview</div><div class="pdf-viewer-scroll"><div class="pdf-preview-loading"><i class="fa fa-file-pdf"></i></div></div>';

  // Keep parent name in heading
  document.getElementById('preview-pdf-name').textContent = parentStem;

  try {
    const res  = await fetch('/admin-panel/api/pdf-preview/?pdf=' + encodeURIComponent(partFilename));
    const data = await res.json();
    if (data.error) { strip.innerHTML = `<div class="pdf-preview-loading">${data.error}</div>`; return; }
    countEl.textContent = data.total + ' pages';
    subEl.textContent   = partFilename;
    strip.innerHTML = data.pages.map((p, i) => `
      <div class="pdf-thumb-item${i===0?' active':''}" onclick="selectPreviewPage(${i})" id="thumb-${i}">
        <img src="${p.thumb}" alt="Page ${p.num}" loading="lazy"/>
        <span class="thumb-num">${p.num}</span>
      </div>`).join('');
    window._previewPages = data.pages;
    if (data.pages.length) renderContinuousPreview(data.pages);
  } catch(e) {
    strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Failed to load preview.</div>';
  }
}

async function previewParentGroup(parentStem, parts) {
  selectedPdf = parts[0];
  _splitStem  = parentStem;

  // Immediately switch progress bar to this PDF before any async calls
  _trackedPdf     = parentStem;
  _trackedStage   = 'uploaded';
  _trackedPercent = null;
  _trackedSetAt   = Date.now();
  _approvedAt     = 0;
  _renderProgress(parentStem, 'uploaded');
  // Build splits array by fetching page counts for each part
  const splits = [];
  // Ensure _pdfDetails is populated
  if (!_pdfDetails.length) {
    try {
      const r = await fetch('/admin-panel/api/pdfs/');
      const d = await r.json();
      _pdfDetails = d.pdf_details || [];
    } catch(e) {}
  }
  for (const partName of parts) {
    try {
      const res  = await fetch(`/admin-panel/api/pdf-pages/?pdf=${encodeURIComponent(partName)}`);
      const data = await res.json();
      const m = partName.match(/_(custom_)?p(\d+)-(\d+)\.pdf$/i);
      const pages = m ? `${parseInt(m[2])}–${parseInt(m[3])}` : '?';
      const detail = _pdfDetails.find(d => d.name === partName);
      const isDone = detail && detail.chunks_count > 0;
      splits.push({ filename: partName, pages, page_count: data.pages || 0, done: isDone });
    } catch(e) {
      splits.push({ filename: partName, pages: '?', page_count: 0, done: false });
    }
  }
  _splitParts = splits;

  // Show the preview section with parent name as heading
  const section = document.getElementById('pdf-preview-section');
  section.style.display = 'block';
  document.getElementById('preview-pdf-name').textContent = parentStem;
  document.getElementById('preview-page-count').textContent = `${parts.length} split parts`;
  document.getElementById('preview-sub').textContent = 'Click a split part \u25b6 to preview its pages';

  // Show splitter section with parts list (skip showSplitterForPdf which resets _splitParts)
  const splitterSection = document.getElementById('splitter-section');
  splitterSection.style.display = 'block';
  splitterSection.classList.remove('collapsed');
  document.getElementById('splitter-body').style.display = 'block';
  document.getElementById('splitter-pdf-name').textContent = parentStem;
  document.getElementById('splitter-pages-badge').textContent = `${parts.length} parts`;
  document.getElementById('splitter-info-bar').style.display = 'flex';

  // Compute proportional progress from existing split parts' stages
  try {
    const approveRes = await fetch('/admin-panel/api/approve-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: parentStem, split_parts: parts })
    });
    const approveData = await approveRes.json();
    if (!approveData.error) {
      const stage = approveData.stage || 'uploaded';
      const doneCount = approveData.done_count !== undefined ? approveData.done_count : null;
      const totalCount = parts.length;
      if (doneCount !== null && doneCount < totalCount) {
        const percent = 25 + (doneCount / totalCount) * 25;
        _trackedPdf   = parentStem;
        _trackedStage = 'uploaded';
        _renderProgress(parentStem, 'uploaded', percent);
        document.getElementById('workflow-progress-sub').textContent =
          `${doneCount} of ${totalCount} parts chunked. Continue processing remaining parts.`;
      } else {
        setTrackedPdf(parentStem, stage);
      }
    }
  } catch(e) {}

  renderSplitParts(splits);
}

async function loadExistingUploads() {
  const list = document.getElementById('upload-file-list');
  if (!list) return;
  list.innerHTML = '';
  try {
    const res  = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    const pdfs = data.pdfs || [];

    // Separate plain PDFs from split parts
    const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
    const groups  = {};  // parentStem -> [splitName, ...]
    const plain   = [];

    pdfs.forEach(name => {
      if (splitRe.test(name)) {
        const parentStem = name.replace(splitRe, '');
        (groups[parentStem] = groups[parentStem] || []).push(name);
      } else {
        plain.push(name);
      }
    });

    // Render plain PDFs
    plain.forEach(name => addFileItem(name, '', 'success', '\u2713 Uploaded'));

    // Render grouped splits
    Object.entries(groups).forEach(([parentStem, parts]) => {
      const group = document.createElement('div');
      group.className = 'file-group';

      // Parent row — same structure as .file-item
      const header = document.createElement('div');
      header.className = 'file-item';
      header.innerHTML = `
        <div class="file-item-icon"><i class="fa fa-file-pdf"></i></div>
        <div class="file-item-info">
          <div class="fname">${escHtml(parentStem)}</div>
          <div class="fsize">${parts.length} split part${parts.length !== 1 ? 's' : ''}</div>
        </div>
        <span class="fstatus success">✓ Uploaded</span>
        <button class="btn btn-sm btn-secondary file-preview-btn" onclick="previewParentGroup('${escHtml(parentStem)}', ${JSON.stringify(parts).replace(/"/g, '&quot;')})" title="Preview pages">
          <i class="fa fa-eye"></i> Preview
        </button>
        <button class="btn btn-sm btn-danger file-delete-btn" onclick="deleteUploadedPdf('${escHtml(parentStem + '.pdf')}', this)" title="Delete PDF">
          <i class="fa fa-trash"></i> Delete
        </button>
        <button class="btn btn-sm btn-secondary file-group-toggle" title="Show split parts">
          <i class="fa fa-chevron-right"></i>
        </button>`;
      header.querySelector('.file-group-toggle').addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = children.style.display === 'none';
        children.style.display = isOpen ? 'flex' : 'none';
        children.style.flexDirection = 'column';
        e.currentTarget.querySelector('i').className = `fa fa-chevron-${isOpen ? 'down' : 'right'}`;
      });

      const children = document.createElement('div');
      children.className = 'file-group-children';
      children.style.display = 'none';
      parts.forEach(partName => {
        const child = document.createElement('div');
        child.className = 'file-item';
        child.innerHTML = `
          <div class="file-item-icon"><i class="fa fa-file-pdf"></i></div>
          <div class="file-item-info"><div class="fname">${escHtml(partName)}</div></div>
          <span class="fstatus success">✓ Uploaded</span>
          <button class="btn btn-sm btn-secondary file-preview-btn" onclick="loadPdfPreview('${escHtml(partName)}')" title="Preview pages">
            <i class="fa fa-eye"></i> Preview
          </button>
          <button class="btn btn-sm btn-danger file-delete-btn" onclick="deleteUploadedPdf('${escHtml(partName)}', this)" title="Delete PDF">
            <i class="fa fa-trash"></i> Delete
          </button>`;
        children.appendChild(child);
      });

      group.appendChild(header);
      group.appendChild(children);
      list.appendChild(group);
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
      item.remove();
      showToast(data.error, 'error');
      return;
    } else {
      setFileStatus(item, 'success', V2_INGEST_ENABLED ? '✓ Queued' : '✓ Uploaded');
      showToast(data.message || 'Uploaded successfully!', 'success');
      // Track progress immediately on upload (25%)
      try {
        const approveRes = await fetch('/admin-panel/api/approve-pdf/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
          body: JSON.stringify({ filename: file.name })
        });
        const approveData = await approveRes.json();
        if (!approveData.error) setTrackedPdf(file.name, approveData.stage || 'uploaded');
      } catch(e) {}
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
    const count = audit.pending_embeddings;
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

  // Immediately switch progress bar to this PDF before any async calls
  _trackedPdf     = filename.replace(/\.pdf$/i, '');
  _trackedStage   = 'uploaded';
  _trackedPercent = null;
  _trackedSetAt   = Date.now();
  _approvedAt     = 0; // clear guard so server can update freely

  // Approve this PDF for progress tracking
  try {
    const approveRes = await fetch('/admin-panel/api/approve-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename })
    });
    const approveData = await approveRes.json();
    if (!approveData.error) {
      setTrackedPdf(filename, approveData.stage || 'uploaded');
    }
  } catch(e) {
    console.error('Failed to approve PDF:', e);
  }

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
  nameEl.textContent    = _splitStem || part.filename;
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
let _activeChunkingBtn = null;

async function loadPdfList() {
  const tbody = document.getElementById('pdf-selector-table-body');
  if (!tbody) return;
  try {
    const res  = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    _pdfDetails = data.pdf_details || [];
    tbody.innerHTML = '';

    const pdfDetails = _pdfDetails.filter(item => item.status === 'Ready');
    if (!pdfDetails.length) {
      tbody.innerHTML = `
        <tr>
          <td colspan="4" style="color:var(--grey);font-size:13px;text-align:center;padding:24px 0">
            No PDFs uploaded yet. <button class="btn btn-sm btn-primary" onclick="showPanel('upload')">Upload one →</button>
          </td>
        </tr>
      `;
      return;
    }

    pdfDetails.forEach(item => {
      const tr = document.createElement('tr');
      tr.dataset.pdf = item.name;
      tr.style.cursor = 'pointer';
      if (selectedPdf === item.name) {
        tr.classList.add('selected');
      }

      // Add click handler to select the row
      tr.addEventListener('click', (e) => {
        if (e.target.closest('button') || e.target.closest('a') || e.target.closest('i')) return;
        selectPdfRow(tr, item.name);
      });

      // Status Badge
      let statusBadge = '';
      if (item.status === 'Ready') {
        statusBadge = `<span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span>`;
      } else if (item.status === 'Processing') {
        statusBadge = `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`;
      } else if (item.status === 'Failed') {
        statusBadge = `<span class="badge badge-red"><i class="fa fa-exclamation-circle"></i> Failed</span>`;
      } else {
        statusBadge = `<span class="badge badge-grey"><i class="fa fa-clock"></i> Not chunked</span>`;
      }

      // Actions Buttons
      let actionButtons = `<div class="table-actions">`;

      // 1. Chunks Drawer Button (always visible, disabled if chunks count is 0)
      const chunksDisabled = item.chunks_count === 0 ? 'disabled' : '';
      actionButtons += `
        <button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(item.name)}')" ${chunksDisabled} title="View chunks">
          <i class="fa fa-layer-group"></i> Chunks
        </button>
      `;
      if (IS_ADMIN && item.chunks_count > 0 && item.document_id) {
        actionButtons += `
          <button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(item.name)})' title="Review product families">
            <i class="fa fa-sitemap"></i> Families
          </button>
        `;
      }

      // 2. Create Embedding OR Create Chunks (VLM Run)
      if (item.status === 'Ready') {
        if (item.has_embeddings) {
          actionButtons += `
            <button class="btn btn-sm embed-btn" disabled title="Already embedded" style="background:#22c55e;color:#fff;opacity:1;cursor:not-allowed">
              <i class="fa fa-check-circle"></i> Embedded
            </button>
          `;
        } else if (item.document_id) {
          actionButtons += `
            <button class="btn btn-sm btn-primary embed-btn" onclick="triggerEmbeddingInline('${item.document_id}', '${escHtml(item.name)}')" title="Generate embeddings and index to Qdrant">
              <i class="fa fa-brain"></i> Create Embedding
            </button>
          `;
        } else {
          actionButtons += `
            <button class="btn btn-sm btn-primary embed-btn" disabled title="No document record found">
              <i class="fa fa-brain"></i> Create Embedding
            </button>
          `;
        }
      } else if (item.status === 'Processing') {
        actionButtons += `
          <button class="btn btn-sm btn-primary embed-btn" disabled>
            <i class="fa fa-spinner fa-spin"></i> Processing
          </button>
        `;
      } else {
        // Pending / Not chunked / Failed -> show Create Chunks button
        actionButtons += `
          <button class="btn btn-sm btn-primary" onclick="runChunkingForPdf('${escHtml(item.name)}', this)" title="Run VLM parser to extract product chunks">
            <i class="fa fa-layer-group"></i> Create Chunks
          </button>
        `;
      }

      // 3. Delete Button
      actionButtons += `
        <button class="btn btn-sm btn-danger" onclick="deleteEmbeddingsOnly('${escHtml(item.name)}')" title="Delete embeddings only">
          <i class="fa fa-trash"></i> Delete
        </button>
      `;

      actionButtons += `</div>`;

      tr.innerHTML = `
        <td>
          <div class="pdf-name-cell" style="display:flex;align-items:center;gap:8px">
            <i class="fa fa-file-pdf" style="color:#ef4444;font-size:16px"></i>
            <span class="pname" style="font-weight:500;font-size:13px">${escHtml(item.name)}</span>
          </div>
        </td>
        <td>
          <span class="badge badge-blue">${item.chunks_count}</span>
        </td>
        <td>${statusBadge}</td>
        <td>${actionButtons}</td>
      `;
      tbody.appendChild(tr);
    });

    // Correct progress bar if the tracked PDF's actual embedding state doesn't match
    if (_trackedPdf && ['indexed', 'tested'].includes(_trackedStage)) {
      const stem = _trackedPdf.replace(/\.pdf$/i, '');
      const trackedDetail = pdfDetails.find(d =>
        d.name === _trackedPdf || d.name === stem + '.pdf' ||
        d.name.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') === stem
      );
      if (trackedDetail && !trackedDetail.has_embeddings) {
        _trackedStage = 'chunked';
        _renderProgress(_trackedPdf, 'chunked');
      }
    }

  } catch(e) {
    console.error("Error loading PDF list: ", e);
  }
}

let selectedPdf = null;
let _familyPanelState = {
  pdf: '',
  document_id: '',
  chunks: [],
  families: [],
  chunksById: {},
  familiesById: {},
  search: '',
};
let _familySelectedChunkIds = new Set();
let _familySelectedId = '';
let _familyPanelAutoLoaded = false;
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

function selectPdfRow(rowEl, name) {
  _highlightPdfRow(name);
  rowEl.classList.add('selected');
  selectedPdf = name;
  _userSelectedPdf = name;
  // Update progress bar to reflect this PDF's stage
  fetch('/admin-panel/api/approve-pdf/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify({ filename: name })
  })
  .then(r => r.json())
  .then(data => {
    if (!data.error) setTrackedPdf(name, data.stage || 'uploaded');
  })
  .catch(() => {});
}

function _highlightPdfRow(name) {
  document.querySelectorAll('#pdf-selector-table-body tr').forEach(r => {
    r.classList.toggle('selected', r.dataset.pdf === name);
  });
}

function _findPdfDetail(name) {
  if (!name) return null;
  const stem = String(name).replace(/\.pdf$/i, '');
  return _pdfDetails.find(item =>
    item.name === name ||
    item.name === `${stem}.pdf` ||
    item.name.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') === stem
  ) || null;
}

function _defaultChunkedPdf() {
  return _pdfDetails.find(item => item.chunks_count > 0) || null;
}

function openFamilies(pdfName) {
  if (pdfName) {
    selectedPdf = pdfName;
    _highlightPdfRow(pdfName);
  }
  showPanel('families');
}

async function runChunkingForPdf(name, btnEl) {
  selectedPdf = name;
  const row = document.querySelector(`#pdf-selector-table-body tr[data-pdf="${name}"]`);
  if (row) {
    selectPdfRow(row, name);
  }
  _activeChunkingBtn = btnEl;
  await runChunking();
}

async function processWholePdf(btnEl) {
  if (!selectedPdf) { showToast('No PDF selected.', 'error'); return; }
  btnEl.disabled = true;
  btnEl.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';
  try {
    const res = await fetch('/admin-panel/api/pipeline/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({
        filename: selectedPdf,
        parsing_instructions: getParsingInstructions()
      })
    });
    const data = await res.json();
    if (data.error) {
      showToast(`Chunking failed: ${getCleanErrorMessage(data.error)}`, 'error', 10000);
    } else {
      showToast('Product chunks created!', 'success');
      advanceTrackedStage('chunked');
      _setWholePdfDone(btnEl);
      refreshStats();
      loadPdfList();
      const nextEl = document.getElementById('next-to-index');
      if (nextEl) nextEl.style.display = 'flex';
      markStepDone('chunk');
    }
  } catch(e) {
    showToast('Request failed.', 'error');
  } finally {
    btnEl.disabled = false;
  }
}

function _setWholePdfDone(btnEl) {
  if (btnEl) {
    btnEl.innerHTML = '<i class="fa fa-redo"></i> Re-run';
  }
  const statusEl = document.getElementById('whole-pdf-status');
  if (statusEl) {
    statusEl.style.display = 'flex';
    statusEl.innerHTML = '<i class="fa fa-check-circle" style="color:#22c55e"></i> <span style="color:#22c55e">Chunks created — ready to index</span>';
  }
}

async function triggerEmbeddingInline(documentId, pdfName) {
  console.log('triggerEmbeddingInline called with:', { documentId, pdfName });
  
  if (!documentId) {
    showToast('No document ID found for this PDF.', 'error');
    return;
  }
  
  try {
    const auditResponse = await fetch(`/admin-panel/api/v2/documents/${documentId}/index-audit/`);
    const audit = await auditResponse.json();
    if (!auditResponse.ok || audit.error) {
      showToast(audit.error || 'Could not load indexing audit.', 'error');
      return;
    }
    if (audit.review_blocked) {
      showToast(`${audit.review_blocked} chunks still require review.`, 'error');
      return;
    }
    const count = audit.pending_embeddings;
    
    if (count === 0) {
      showToast('All chunks are already indexed.', 'info');
      return;
    }
    
    const row = document.querySelector(`#pdf-selector-table-body tr[data-pdf="${pdfName}"]`);
    const embedBtn = row ? row.querySelector('.embed-btn') : null;
    if (embedBtn) {
      embedBtn.disabled = true;
      embedBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Indexing…';
    }
    
    try {
      const response = await fetch(`/admin-panel/api/v2/documents/${documentId}/index/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
        body: JSON.stringify({ confirmed_embedding_count: count })
      });
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || 'Indexing failed.');
      
      // Update progress to 'indexed' (75%)
      try {
        const stageRes = await fetch('/admin-panel/api/update-pdf-stage/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
          body: JSON.stringify({ filename: pdfName, stage: 'indexed' })
        });
        const stageData = await stageRes.json();
        console.log('Stage update response:', stageData);
        
        if (stageRes.ok) {
          console.log('Successfully updated PDF stage to indexed');
        } else {
          console.error('Failed to update stage:', stageData);
        }
      } catch(e) {
        console.error('Failed to update stage:', e);
      }
      
      showToast(`Indexed ${data.indexed} chunk(s) for "${pdfName}".`, 'success');

      // Immediately render 75% — do not rely on refreshStats which may return stale 'tested'
      const displayPdf = _trackedPdf || pdfName.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || pdfName;
      _trackedPdf   = displayPdf;
      _trackedStage = 'indexed';
      _trackedPercent = null;
      _trackedSetAt = Date.now();
      _renderProgress(displayPdf, 'indexed');
      await loadPdfList();
      if (document.getElementById('panel-index')?.classList.contains('active')) {
        await loadIndexPanel();
      }
    } catch(error) {
      showToast(error.message, 'error');
      if (embedBtn) {
        embedBtn.disabled = false;
        embedBtn.innerHTML = '<i class="fa fa-brain"></i> Create Embedding';
      }
    }
  } catch(e) {
    showToast('Failed to perform index audit: ' + e.message, 'error');
  }
}

// ── Create Chunks (Pipeline) ──────────────────────────────────────────────────
async function runChunking() {
  if (!selectedPdf) { showToast('Please select a PDF first.', 'error'); return; }

  const btn   = _activeChunkingBtn || document.getElementById('btn-run-chunk');
  const prog  = document.getElementById('chunk-progress');
  const fill  = document.getElementById('chunk-fill');
  const pct   = document.getElementById('chunk-pct');
  const log   = document.getElementById('chunk-log');
  const label = document.getElementById('chunk-progress-label');

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';
  }
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
      showToast(`Chunking failed: ${getCleanErrorMessage(data.error)}`, 'error', 10000);
    } else {
      showToast('Product chunks created!', 'success');
      advanceTrackedStage('chunked');
      refreshStats();
      loadPdfList();
      document.getElementById('next-to-index').style.display = 'flex';
      markStepDone('chunk');
    }
    prog.classList.remove('visible');
    log.classList.remove('visible');
    fill.style.width = '0%';
    pct.textContent = '0%';
    label.textContent = 'Extracting products…';
  } catch(e) {
    clearInterval(ticker);
    showToast('Request failed.', 'error');
    prog.classList.remove('visible');
    log.classList.remove('visible');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fa fa-layer-group"></i> Create Chunks';
    }
    _activeChunkingBtn = null;
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
  const providerLabel = provider === 'openai' ? 'OpenAI' : 'Google Gemini';
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

async function deleteEmbeddingsOnly(filename) {
  if (!confirm(`Delete "${filename}" and all its associated data?`)) return;
  try {
    const res  = await fetch('/admin-panel/api/delete-embeddings/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast(data.message, 'success');
    // Immediately downgrade progress bar to chunked (50%)
    const stem = filename.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '').replace(/\.pdf$/i, '');
    const displayPdf = (_trackedPdf && (_trackedPdf === stem || _trackedPdf === filename)) ? _trackedPdf : stem;
    _trackedPdf   = displayPdf;
    _trackedStage = 'chunked';
    _trackedSetAt = Date.now();
    _renderProgress(displayPdf, 'chunked');
    loadPdfList();
    if (typeof loadIndexPanel === 'function') loadIndexPanel();
  } catch(e) { showToast('Delete failed.', 'error'); }
}

// -- Stats refresh -------------------------------------------------------------────────────
function updateCatalogTable(processed) {
  const container = document.getElementById('catalog-overview-body');
  if (!container) return;

  // If there are no processed PDFs, show the empty state
  if (!processed.length) {
    let emptyHtml = `
      <div style="text-align:center;padding:48px 20px;color:var(--grey)">
        <i class="fa fa-inbox" style="font-size:40px;margin-bottom:14px;display:block;color:#d0d0d0"></i>
        <p style="font-size:14px;font-weight:600;margin-bottom:8px">No PDFs processed yet</p>
    `;
    if (IS_ADMIN) {
      emptyHtml += `
        <p style="font-size:13px;margin-bottom:20px">Follow the 5-step workflow to get started.</p>
        <button class="btn btn-primary" onclick="showPanel('upload')">
          <i class="fa fa-upload"></i> Upload Your First PDF
        </button>
      `;
    } else {
      emptyHtml += `
        <p style="font-size:13px">No catalog data available yet. Please contact your administrator.</p>
      `;
    }
    emptyHtml += `</div>`;
    container.innerHTML = emptyHtml;
    return;
  }

  // Otherwise, construct the table
  let tableHtml = `
    <div class="table-wrap">
      <table class="catalog-table">
        <thead>
          <tr>
            <th>PDF Name</th>
            <th>Chunks</th>
            <th>Products</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
  `;

  // 1. Processed rows
  processed.forEach(item => {
    tableHtml += `
      <tr>
        <td>
          <div class="pdf-name-cell">
            <i class="fa fa-file-pdf"></i>
            <span class="pname">${escHtml(item.name)}</span>
          </div>
        </td>
        <td><span class="badge badge-blue">${item.chunks}</span></td>
        <td><span class="badge badge-green">${item.products}</span></td>
        <td><span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span></td>
        <td>
          <div class="table-actions">
            <button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(item.name)}')">
              <i class="fa fa-layer-group"></i> Chunks
            </button>
            ${IS_ADMIN && item.chunks > 0 ? `
            <button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(item.name)})'>
              <i class="fa fa-sitemap"></i> Families
            </button>` : ''}
            <button class="btn btn-sm btn-secondary" onclick="showPanel('chat')">
              <i class="fa fa-comments"></i> Test
            </button>
    `;
    if (IS_ADMIN) {
      tableHtml += `
            <button class="btn btn-sm btn-danger" onclick="deletePdf('${escHtml(item.name)}')">
              <i class="fa fa-trash"></i> Delete
            </button>
      `;
    }
    tableHtml += `
          </div>
        </td>
      </tr>
    `;
  });

  tableHtml += `
        </tbody>
      </table>
    </div>
  `;

  container.innerHTML = tableHtml;
}

async function refreshStats() {
  try {
    const res  = await fetch('/admin-panel/api/stats/');
    const data = await res.json();
    console.log('Stats received:', { 
      pdf_progress: data.pdf_progress, 
      approved_pdfs: data.approved_pdfs,
      tracked_pdf: data.tracked_pdf,
      tracked_stage: data.tracked_stage,
      tracked_percent: data.tracked_percent,
      total_split_parts: data.total_split_parts
    });
    document.getElementById('stat-pdfs').textContent      = data.total_pdfs ?? '—';
    document.getElementById('stat-processed').textContent = (data.processed||[]).length;
    document.getElementById('stat-indexed').textContent   = data.indexed ?? '—';
    document.getElementById('stat-chunks').textContent    = data.total_chunks ?? data.indexed ?? '—';
    updateWorkflowProgress(data);
    updateCatalogTable(data.processed || []);
  } catch(e) {
    console.error('Error refreshing stats:', e);
  }
}

function _num(value) {
  const parsed = parseInt(String(value ?? '0').replace(/[^0-9]/g, ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

// ── Per-PDF progress tracking ─────────────────────────────────────────────────
// stages: uploaded=25%  chunked=50%  families=70%  indexed=85%  tested=100%
let _trackedPdf     = null;
let _trackedStage   = null;
let _trackedPercent = null;  // custom percent for partial-split progress
let _trackedSetAt   = 0;     // timestamp when _trackedPdf was last set locally
let _userSelectedPdf = null; // PDF explicitly selected by user clicking a row

const STAGE_CONFIG = {
  uploaded: { percent: 25,  stepNo: 1, active: 'upload',   title: 'PDF uploaded',        sub: 'Preview the PDF then split & parse to extract product chunks.' },
  chunked:  { percent: 50,  stepNo: 2, active: 'chunk',    title: 'Chunks created',       sub: 'Product chunks are ready. Review product families before indexing.' },
  families: { percent: 70,  stepNo: 3, active: 'families', title: 'Product families',     sub: 'Group chunks into canonical families and approve the final mapping.' },
  indexed:  { percent: 85,  stepNo: 4, active: 'index',    title: 'Indexed & embedded',   sub: 'Approved families are indexed for semantic search.' },
  tested:   { percent: 100, stepNo: 5, active: 'chat',     title: 'Catalog ready!',        sub: 'All steps complete. The catalog is live for product queries.' },
};

function setTrackedPdf(filename, stage) {
  _trackedPdf     = filename;
  _trackedStage   = stage;
  _trackedPercent = null;
  _trackedSetAt   = Date.now();
  _renderProgress(filename, stage);
}

function advanceTrackedStage(stage, fallbackPdf) {
  const pdf = _trackedPdf || fallbackPdf || null;
  if (!pdf) return;
  const order = ['uploaded', 'chunked', 'families', 'indexed', 'tested'];
  const cur = _trackedStage ? order.indexOf(_trackedStage) : -1;
  if (order.indexOf(stage) > cur) {
    _trackedPdf   = pdf;
    _trackedStage = stage;
    _trackedSetAt = Date.now();
    _renderProgress(pdf, stage);
    fetch('/admin-panel/api/update-pdf-stage/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: pdf, stage })
    }).catch(() => {});
  }
}

function _renderProgress(filename, stage, customPercent) {
  const cfg = STAGE_CONFIG[stage];
  if (!cfg) return;
  const percent = (customPercent !== undefined) ? customPercent : cfg.percent;
  _trackedPercent = (customPercent !== undefined) ? customPercent : null;
  document.getElementById('workflow-step-label').textContent   = `Step ${cfg.stepNo} of 5`;
  document.getElementById('workflow-progress-title').textContent = cfg.title;
  document.getElementById('workflow-progress-sub').textContent   = cfg.sub;
  document.getElementById('workflow-progress-percent').textContent = `${Math.round(percent)}%`;
  document.getElementById('workflow-progress-fill').style.width   = `${percent}%`;

  const nameBar = document.getElementById('progress-pdf-name-bar');
  const nameEl  = document.getElementById('progress-pdf-name');
  if (filename) { nameEl.textContent = filename; nameBar.style.display = 'flex'; }
  else          { nameBar.style.display = 'none'; }

  const doneMap = { upload: false, chunk: false, families: false, index: false, chat: false };
  if (stage === 'uploaded') { doneMap.upload = true; }
  if (stage === 'chunked')  { doneMap.upload = doneMap.chunk = true; }
  if (stage === 'families') { doneMap.upload = doneMap.chunk = doneMap.families = true; }
  if (stage === 'indexed')  { doneMap.upload = doneMap.chunk = doneMap.families = doneMap.index = true; }
  if (stage === 'tested')   { doneMap.upload = doneMap.chunk = doneMap.families = doneMap.index = doneMap.chat = true; }

  document.querySelectorAll('[data-progress-step]').forEach(el => {
    const key = el.dataset.progressStep;
    el.classList.toggle('done',   Boolean(doneMap[key]));
    el.classList.toggle('active', key === cfg.active && !doneMap[key]);
  });
}

function _resetProgress() {
  _trackedPdf     = null;
  _trackedStage   = null;
  _trackedPercent = null;
  document.getElementById('workflow-step-label').textContent    = 'Step 0 of 5';
  document.getElementById('workflow-progress-title').textContent = 'Catalog setup progress';
  document.getElementById('workflow-progress-sub').textContent   = 'Upload a PDF to start the catalog pipeline.';
  document.getElementById('workflow-progress-percent').textContent = '0%';
  document.getElementById('workflow-progress-fill').style.width  = '0%';
  document.getElementById('progress-pdf-name-bar').style.display = 'none';
  document.querySelectorAll('[data-progress-step]').forEach(el => el.classList.remove('done','active'));
}

function updateWorkflowProgress(data = {}) {
  if (data.tracked_pdf && data.tracked_stage) {
    // Never downgrade from 'tested' — local state is source of truth after approve button
    const STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested'];
    if (_trackedStage === 'tested' && STAGE_ORDER.indexOf(data.tracked_stage) < STAGE_ORDER.indexOf('tested')) {
      return; // Keep current 100% state
    }
    // If user explicitly selected a row, don't let server overwrite with a different PDF
    if (_userSelectedPdf && _userSelectedPdf !== data.tracked_pdf) {
      return;
    }
    // If user just switched to a new PDF locally (within 5s), ignore stale server data
    if (_trackedPdf && _trackedPdf !== data.tracked_pdf && (Date.now() - _trackedSetAt) < 5000) {
      return;
    }
    _trackedPdf   = data.tracked_pdf;
    _trackedStage = data.tracked_stage;
    // Prefer locally-computed _trackedPercent (from runSingleSplit) over server value
    const pct = _trackedPercent !== null ? _trackedPercent
              : (data.tracked_percent !== undefined ? data.tracked_percent : undefined);
    if (pct !== undefined) {
      _renderProgress(data.tracked_pdf, data.tracked_stage, pct);
      const totalParts = data.total_split_parts || null;
      if (totalParts) {
        let sub;
        if (pct <= 50) {
          const chunkedParts = Math.round((pct - 25) / 25 * totalParts);
          sub = `${chunkedParts} of ${totalParts} parts chunked. Continue processing remaining parts.`;
        } else {
          const indexedParts = Math.round((pct - 50) / 25 * totalParts);
          sub = `${indexedParts} of ${totalParts} parts indexed. Continue embedding remaining parts.`;
        }
        document.getElementById('workflow-progress-sub').textContent = sub;
      }
    } else {
      _renderProgress(data.tracked_pdf, data.tracked_stage);
    }
  } else if (!_trackedPdf) {
    _resetProgress();
  }
}

function approvePdfForTracking() {
  const filename = document.getElementById('preview-pdf-name').textContent.trim();
  if (!filename) return;
  const btn = document.getElementById('btn-approve-pdf');
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Approving…';
  fetch('/admin-panel/api/approve-pdf/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify({ filename })
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) { showToast(data.error, 'error'); return; }
    setTrackedPdf(filename, data.stage || 'uploaded');
    showToast(`Tracking progress for "${filename}".`, 'success');
    btn.innerHTML = '<i class="fa fa-check-circle"></i> Approved';
  })
  .catch(() => showToast('Approve failed.', 'error'))
  .finally(() => { btn.disabled = false; });
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
  if (wholeBtn) { wholeBtn.style.display = ''; wholeBtn.innerHTML = '<i class="fa fa-layer-group"></i> Process Whole PDF'; }
  // Show Done/Pending status for non-split PDF
  const wholePdfStatus = document.getElementById('whole-pdf-status');
  if (wholePdfStatus) {
    if (!_pdfDetails.length) {
      fetch('/admin-panel/api/pdfs/').then(r => r.json()).then(d => {
        _pdfDetails = d.pdf_details || [];
        _updateWholePdfStatus(filename);
      }).catch(() => {});
    } else {
      _updateWholePdfStatus(filename);
    }
  }
  document.getElementById('splitter-preview').className = 'splitter-preview';
  document.getElementById('splitter-preview').innerHTML = '';
  document.getElementById('splitter-pdf-name').textContent = filename;
  document.getElementById('splitter-pages-badge').innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
  document.getElementById('splitter-info-bar').style.display = 'flex';
  _loadPageCount(filename);
}


let _splitStem    = null;
let _splitParts   = [];
let _pdfDetails   = [];
let _pagesPerPart = 5;

function _updateWholePdfStatus(filename) {
  const el = document.getElementById('whole-pdf-status');
  const wholeBtn = document.getElementById('btn-process-whole-pdf');
  if (!el) return;
  const detail = _pdfDetails.find(d => d.name === filename);
  if (detail && detail.chunks_count > 0) {
    el.style.display = 'flex';
    el.innerHTML = `<i class="fa fa-check-circle" style="color:#22c55e"></i> <span style="color:#22c55e">Done &mdash; ${detail.chunks_count} chunk${detail.chunks_count !== 1 ? 's' : ''} created</span>`;
    if (wholeBtn) wholeBtn.innerHTML = '<i class="fa fa-redo"></i> Re-run';
  } else {
    el.style.display = 'none';
    el.innerHTML = '';
    if (wholeBtn) wholeBtn.innerHTML = '<i class="fa fa-layer-group"></i> Process Whole PDF';
  }
}

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
  updateWorkflowProgress({ pdf_progress: {}, approved_pdfs: [] });
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
  const wholePdfStatus = document.getElementById('whole-pdf-status');
  if (wholePdfStatus) { wholePdfStatus.style.display = 'none'; wholePdfStatus.innerHTML = ''; }
  list.innerHTML = parts.map((p, i) => {
    const isDone = !!p.done;
    const statusCls  = isDone ? 'done'    : 'pending';
    const statusText = isDone ? '\u2713 Done' : 'Pending';
    const itemCls    = isDone ? 'split-part-item done' : 'split-part-item';
    const runBtn     = isDone
      ? `<button class="btn btn-sm btn-secondary" id="split-btn-${i}" onclick="event.stopPropagation();runSingleSplit(${i})" title="Re-run"><i class="fa fa-redo"></i></button>`
      : `<button class="btn btn-sm btn-secondary" id="split-btn-${i}" onclick="event.stopPropagation();runSingleSplit(${i})"><i class="fa fa-play"></i> Run</button>`;
    return `
    <div class="${itemCls}" id="split-part-${i}" onclick="loadSplitPartPreview(${i})" title="Preview this split part">
      <div class="split-part-num">${i+1}</div>
      <div class="split-part-info">
        <div class="part-name">${escHtml(p.filename)}</div>
        <div class="part-pages">Pages ${escHtml(p.pages)} &nbsp;&middot;&nbsp; ${p.page_count} page${p.page_count!==1?'s':''}</div>
      </div>
      <span class="split-part-status ${statusCls}" id="split-status-${i}">${statusText}</span>
      <button class="btn btn-sm btn-secondary split-preview-btn" onclick="event.stopPropagation();loadSplitPartPreview(${i})">
        <i class="fa fa-eye"></i>
      </button>
      ${runBtn}
    </div>`;
  }).join('');
  if (parts.length) loadSplitPartPreview(0);
}

async function runSingleSplit(idx) {
  const part   = _splitParts[idx];
  const itemEl = document.getElementById(`split-part-${idx}`);
  const statEl = document.getElementById(`split-status-${idx}`);
  const btnEl  = document.getElementById(`split-btn-${idx}`);
  const processAllBtn = document.getElementById('btn-run-all-splits');

  itemEl.className = 'split-part-item running';
  statEl.className = 'split-part-status running';
  statEl.textContent = 'Running…';
  btnEl.disabled = true;
  btnEl.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
  if (processAllBtn) { processAllBtn.disabled = true; processAllBtn.style.opacity = '0.5'; }

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
      btnEl.innerHTML = '<i class="fa fa-redo"></i>';
      
      // Update stage for this split part
      try {
        await fetch('/admin-panel/api/update-pdf-stage/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
          body: JSON.stringify({ filename: part.filename, stage: 'chunked' })
        });
      } catch(e) {}

      // Proportional progress: 25% + (doneParts/totalParts) * 25%
      if (_splitStem && _splitParts.length) {
        const doneParts = _splitParts.filter((_, i) => {
          const el = document.getElementById(`split-status-${i}`);
          return el && el.textContent === '✓ Done';
        }).length;
        const totalParts = _splitParts.length;
        const percent = 25 + (doneParts / totalParts) * 25;
        const sub = doneParts < totalParts
          ? `${doneParts} of ${totalParts} parts chunked. Continue processing remaining parts.`
          : 'All parts chunked. Now index & embed for semantic search.';
        _trackedPdf   = _splitStem;
        _trackedStage = doneParts < totalParts ? 'uploaded' : 'chunked';
        _trackedSetAt = Date.now();
        _renderProgress(_splitStem, 'uploaded', percent);
        document.getElementById('workflow-progress-sub').textContent = sub;
        if (doneParts === totalParts) {
          // Persist full chunked stage to DB
          fetch('/admin-panel/api/update-pdf-stage/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
            body: JSON.stringify({ filename: _splitStem, stage: 'chunked' })
          }).catch(() => {});
        }
      }

      _trackedPercent = null;  // let server recompute fresh percent
      await refreshStats();
      loadPdfList();
    }
  } catch(e) {
    itemEl.className = 'split-part-item errored';
    statEl.className = 'split-part-status errored';
    statEl.textContent = '✗ Error';
    showToast(`Network error on part ${idx+1}.`, 'error', 10000);
  } finally {
    const currentBtn = document.getElementById(`split-btn-${idx}`);
    if (currentBtn) {
      if (currentBtn.innerHTML.includes('redo')) {
        currentBtn.disabled = false;
      } else {
        currentBtn.innerHTML = '<i class="fa fa-play"></i> Run';
        currentBtn.disabled = false;
      }
    }
    const processAllBtn2 = document.getElementById('btn-run-all-splits');
    if (processAllBtn2) { processAllBtn2.disabled = false; processAllBtn2.style.opacity = ''; }
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
      <div class="chunk-edit-pane" id="chunk-edit-pane-${i}" style="display:none">
        <textarea class="chunk-editor" id="chunk-editor-${i}" oninput="updateChunkPreview(${i})">${escHtml(c.content)}</textarea>
        <div class="chunk-edit-preview" id="chunk-preview-${i}">${mdToHtml(c.content)}</div>
      </div>
    </div>`).join('');
}

function editChunk(idx) {
  document.getElementById(`chunk-rendered-${idx}`).style.display = 'none';
  document.getElementById(`chunk-edit-pane-${idx}`).style.display = 'block';
  const preview = document.getElementById(`chunk-preview-${idx}`);
  preview.contentEditable = 'true';
  // Re-render cleanly so spacing is tight (no stale browser-injected divs)
  const pdfName = document.getElementById('chunks-drawer-title').textContent;
  const chunk = _chunksCache[pdfName]?.[idx];
  preview.innerHTML = mdToHtml(chunk?.content || '');
  preview.focus();
  document.getElementById(`chunk-edit-${idx}`).style.display = 'none';
  document.getElementById(`chunk-save-${idx}`).style.display = 'inline-flex';
  document.getElementById(`chunk-cancel-${idx}`).style.display = 'inline-flex';
}

function cancelEditChunk(idx) {
  const pdfName = document.getElementById('chunks-drawer-title').textContent;
  const chunk = _chunksCache[pdfName]?.[idx];
  const preview = document.getElementById(`chunk-preview-${idx}`);
  preview.contentEditable = 'false';
  preview.innerHTML = mdToHtml(chunk?.content || '');
  document.getElementById(`chunk-rendered-${idx}`).style.display = 'block';
  document.getElementById(`chunk-edit-pane-${idx}`).style.display = 'none';
  document.getElementById(`chunk-edit-${idx}`).style.display = 'inline-flex';
  document.getElementById(`chunk-save-${idx}`).style.display = 'none';
  document.getElementById(`chunk-cancel-${idx}`).style.display = 'none';
}

function updateChunkPreview(idx) {
  // no-op: preview is now contenteditable, no textarea to sync
}

async function saveChunk(idx) {
  const pdfName = document.getElementById('chunks-drawer-title').textContent;
  const chunk = _chunksCache[pdfName]?.[idx];
  if (!chunk) return;
  // Read edited content from the contenteditable preview div as plain text
  const preview = document.getElementById(`chunk-preview-${idx}`);
  const content = preview.innerText || preview.textContent || '';
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
    preview.contentEditable = 'false';
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

// ── Product Families ────────────────────────────────────────────────────────
function _familyStatusLabel(status) {
  const labels = {
    approved: 'Approved',
    needs_review: 'Needs review',
    rejected: 'Rejected',
  };
  return labels[status] || 'Unassigned';
}

function _familyStatusBadge(status) {
  const key = status || 'muted';
  return `<span class="family-badge ${escAttr(key)}">${escHtml(_familyStatusLabel(status))}</span>`;
}

function _familySelectedChunks() {
  return [..._familySelectedChunkIds]
    .map(id => _familyPanelState.chunksById[id])
    .filter(Boolean);
}

function _familySelectedRecord() {
  return _familySelectedId ? (_familyPanelState.familiesById[_familySelectedId] || null) : null;
}

function _familyPageLabel(chunk) {
  const start = Number(chunk.page_start || 0);
  const end = Number(chunk.page_end || chunk.page_start || 0);
  if (!start && !end) return '—';
  return start === end ? `Page ${start}` : `Pages ${start}-${end}`;
}

function _familyResetForm() {
  _familySelectedId = '';
  _familySelectedChunkIds = new Set();
  const id = document.getElementById('family-id');
  const name = document.getElementById('family-name');
  const code = document.getElementById('family-code');
  const category = document.getElementById('family-category');
  const status = document.getElementById('family-review-status');
  if (id) id.value = '';
  if (name) name.value = '';
  if (code) code.value = '';
  if (category) category.value = '';
  if (status) status.value = 'approved';
}

function renderFamilySelectionSummary() {
  const badge = document.getElementById('family-editor-chunk-count');
  const summary = document.getElementById('family-selection-summary');
  const selected = _familySelectedChunks();
  const family = _familySelectedRecord();

  if (badge) {
    badge.textContent = `${selected.length} selected`;
  }

  if (!summary) return;
  if (!selected.length) {
    summary.innerHTML = 'No chunks selected.';
    return;
  }

  const familyNames = [...new Set(selected.map(chunk => chunk.family_name).filter(Boolean))];
  const pageStarts = selected.map(chunk => Number(chunk.page_start || 0)).filter(Boolean);
  const pageEnds = selected.map(chunk => Number(chunk.page_end || chunk.page_start || 0)).filter(Boolean);
  const pageStart = pageStarts.length ? Math.min(...pageStarts) : 0;
  const pageEnd = pageEnds.length ? Math.max(...pageEnds) : 0;
  const bits = [];

  if (family) {
    bits.push(`Editing <strong>${escHtml(family.product_name)}</strong>`);
  }
  bits.push(`<strong>${selected.length}</strong> chunk${selected.length === 1 ? '' : 's'} selected`);
  if (familyNames.length === 1) {
    bits.push(`Source family: <strong>${escHtml(familyNames[0])}</strong>`);
  } else if (familyNames.length > 1) {
    bits.push(`<strong>${familyNames.length}</strong> source families`);
  } else {
    bits.push('Unassigned chunks');
  }
  if (pageStart && pageEnd) {
    bits.push(pageStart === pageEnd ? `Page ${pageStart}` : `Pages ${pageStart}-${pageEnd}`);
  }

  summary.innerHTML = bits.join(' &middot; ');
}

function renderFamilyChunks() {
  const body = document.getElementById('family-chunks-table-body');
  if (!body) return;

  const chunks = _familyPanelState.chunks || [];
  if (!chunks.length) {
    body.innerHTML = `
      <tr>
        <td colspan="7" class="index-table-empty">
          <i class="fa fa-sitemap" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
          No chunks found for this PDF.
        </td>
      </tr>
    `;
    return;
  }

  body.innerHTML = chunks.map(chunk => {
    const selected = _familySelectedChunkIds.has(chunk.id);
    const familyName = chunk.family_name || 'Unassigned';
    const familyCode = chunk.family_code || '';
    const familyStatus = chunk.family_status || '';
    const familyMeta = chunk.family_id
      ? `
        <span class="family-badge ${escAttr(familyStatus || 'muted')}">${escHtml(_familyStatusLabel(familyStatus))}</span>
        ${familyCode ? `<span class="badge badge-grey">${escHtml(familyCode)}</span>` : ''}
      `
      : `<span class="family-badge muted">Unassigned</span>`;
    const searchText = [
      chunk.id,
      chunk.filename,
      chunk.product_name,
      chunk.family_name,
      chunk.family_code,
      chunk.family_status,
      chunk.excerpt,
      chunk.page_start,
      chunk.page_end,
    ].join(' ').toLowerCase();

    return `
      <tr class="${selected ? 'selected' : ''}" data-chunk-id="${escAttr(chunk.id)}" data-search="${escAttr(searchText)}" onclick='familyRowToggle(event, ${JSON.stringify(chunk.id)})'>
        <td class="family-row-check">
          <input type="checkbox" ${selected ? 'checked' : ''} onchange='toggleFamilyChunkSelection(${JSON.stringify(chunk.id)}, this.checked)' />
        </td>
        <td class="family-chunk-id">${escHtml(`C${String(chunk.ordinal || 0).padStart(3, '0')}`)}</td>
        <td class="family-product">
          ${escHtml(chunk.product_name || 'General Info')}
          <small>${escHtml(chunk.filename || '')}</small>
        </td>
        <td class="family-chunk-family">
          <div class="family-name">${escHtml(familyName)}</div>
          <div class="family-meta">
            ${familyMeta}
          </div>
        </td>
        <td>${escHtml(_familyPageLabel(chunk))}</td>
        <td class="family-excerpt">${escHtml(chunk.excerpt || '')}</td>
        <td class="family-status">
          ${chunk.status === 'Embedded'
            ? '<span class="badge badge-green"><i class="fa fa-database"></i> Embedded</span>'
            : '<span class="badge badge-grey"><i class="fa fa-check-circle"></i> Ready</span>'}
        </td>
      </tr>
    `;
  }).join('');

  handleFamilySearch();
}

function renderFamilyCards() {
  const list = document.getElementById('family-card-list');
  const badge = document.getElementById('family-count-badge');
  if (!list) return;

  const families = _familyPanelState.families || [];
  if (badge) badge.textContent = String(families.length);

  if (!families.length) {
    list.innerHTML = '<div class="family-card-empty">No product families created yet for this PDF.</div>';
    return;
  }

  list.innerHTML = families.map(family => {
    const active = _familySelectedId === family.id;
    const chunkPreview = (family.chunks || []).slice(0, 3).map(chunk => `C${String(chunk.ordinal || 0).padStart(3, '0')}`).join(', ');
    const more = family.chunk_count > 3 ? ` +${family.chunk_count - 3} more` : '';
    const pageText = family.page_start && family.page_end
      ? (family.page_start === family.page_end ? `Page ${family.page_start}` : `Pages ${family.page_start}-${family.page_end}`)
      : 'Page range unset';

    return `
      <div class="family-card${active ? ' active' : ''}" onclick='loadFamilyFromCard(${JSON.stringify(family.id)})'>
        <div class="family-card-top">
          <div>
            <div class="family-card-title">${escHtml(family.product_name || 'Unnamed family')}</div>
            <div class="family-card-sub">${escHtml(family.category || 'Uncategorized')} · ${escHtml(pageText)}</div>
          </div>
          ${_familyStatusBadge(family.review_status)}
        </div>
        <div class="family-card-meta">
          <span class="badge badge-blue">${family.chunk_count} chunk${family.chunk_count === 1 ? '' : 's'}</span>
          ${family.product_code ? `<span class="badge badge-grey">${escHtml(family.product_code)}</span>` : ''}
          ${chunkPreview ? `<span class="badge badge-grey">${escHtml(chunkPreview)}${escHtml(more)}</span>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function renderFamilyWorkspace() {
  const familyPdf = document.getElementById('family-panel-pdf');
  if (familyPdf) {
    familyPdf.textContent = _familyPanelState.pdf || 'Select a chunked PDF to review families';
  }
  renderFamilyChunks();
  renderFamilyCards();
  renderFamilySelectionSummary();
}

function familyRowToggle(event, chunkId) {
  if (event && event.target && event.target.closest('input,button,label,a,select,option')) return;
  toggleFamilyChunkSelection(chunkId, !_familySelectedChunkIds.has(chunkId));
}

function toggleFamilyChunkSelection(chunkId, checked) {
  const normalizedId = String(chunkId);
  if (checked) {
    _familySelectedChunkIds.add(normalizedId);
  } else {
    _familySelectedChunkIds.delete(normalizedId);
  }
  renderFamilyChunks();
  renderFamilySelectionSummary();
}

function loadFamilyFromCard(familyId) {
  const family = _familyPanelState.familiesById[String(familyId)];
  if (!family) return;
  _familySelectedId = family.id;
  _familySelectedChunkIds = new Set((family.chunks || []).map(chunk => String(chunk.id)));

  const id = document.getElementById('family-id');
  const name = document.getElementById('family-name');
  const code = document.getElementById('family-code');
  const category = document.getElementById('family-category');
  const status = document.getElementById('family-review-status');
  if (id) id.value = family.id;
  if (name) name.value = family.product_name || '';
  if (code) code.value = family.product_code || '';
  if (category) category.value = family.raw_category || '';
  if (status) status.value = family.review_status || 'approved';

  renderFamilyWorkspace();
}

function clearFamilyForm() {
  _familyResetForm();
  renderFamilyWorkspace();
}

function handleFamilySearch() {
  const input = document.getElementById('family-chunk-search');
  if (!input) return;
  const q = input.value.trim().toLowerCase();
  _familyPanelState.search = q;
  document.querySelectorAll('#family-chunks-table-body tr[data-chunk-id]').forEach(row => {
    const haystack = row.dataset.search || '';
    row.style.display = !q || haystack.includes(q) ? '' : 'none';
  });
}

async function loadFamilyPanel(force = false) {
  if (!IS_ADMIN) return;
  const body = document.getElementById('family-chunks-table-body');
  const cardList = document.getElementById('family-card-list');
  if (!body || !cardList) return;

  try {
    if (!_pdfDetails.length || force) {
      const res = await fetch('/admin-panel/api/pdfs/');
      const data = await res.json();
      if (res.ok && !data.error) {
        _pdfDetails = data.pdf_details || [];
      }
    }

    let pdfName = selectedPdf;
    let detail = _findPdfDetail(pdfName);
    if (!detail || !detail.chunks_count) {
      detail = _defaultChunkedPdf();
      if (detail) {
        pdfName = detail.name;
        selectedPdf = pdfName;
        _highlightPdfRow(pdfName);
      }
    }

    if (!pdfName || !detail || !detail.chunks_count) {
      _familyPanelState = {
        pdf: '',
        document_id: '',
        chunks: [],
        families: [],
        chunksById: {},
        familiesById: {},
        search: '',
      };
      _familyResetForm();
      renderFamilyWorkspace();
      return;
    }

    const res = await fetch(`/admin-panel/api/families/?pdf=${encodeURIComponent(pdfName)}`);
    const data = await res.json();
    if (!res.ok || data.error) {
      throw new Error(data.error || 'Could not load product families.');
    }

    const nextState = {
      pdf: data.pdf || pdfName,
      document_id: data.document_id || '',
      chunks: data.chunks || [],
      families: data.families || [],
      chunksById: Object.fromEntries((data.chunks || []).map(chunk => [String(chunk.id), chunk])),
      familiesById: Object.fromEntries((data.families || []).map(family => [String(family.id), family])),
      search: document.getElementById('family-chunk-search')?.value || '',
    };

    _familyPanelState = nextState;
    if (selectedPdf !== nextState.pdf) {
      selectedPdf = nextState.pdf;
      _highlightPdfRow(selectedPdf);
    }

    if (_familySelectedId && !_familyPanelState.familiesById[_familySelectedId]) {
      _familyResetForm();
    } else if (_familySelectedId) {
      const family = _familyPanelState.familiesById[_familySelectedId];
      _familySelectedChunkIds = new Set((family.chunks || []).map(chunk => String(chunk.id)));
      const id = document.getElementById('family-id');
      const name = document.getElementById('family-name');
      const code = document.getElementById('family-code');
      const category = document.getElementById('family-category');
      const status = document.getElementById('family-review-status');
      if (id) id.value = family.id;
      if (name) name.value = family.product_name || '';
      if (code) code.value = family.product_code || '';
      if (category) category.value = family.raw_category || '';
      if (status) status.value = family.review_status || 'approved';
    } else {
      const validChunkIds = new Set(Object.keys(_familyPanelState.chunksById));
      _familySelectedChunkIds = new Set([..._familySelectedChunkIds].filter(id => validChunkIds.has(String(id))));
    }

    renderFamilyWorkspace();
  } catch (error) {
    body.innerHTML = `
      <tr>
        <td colspan="7" class="index-table-empty" style="color:var(--orange)">
          <i class="fa fa-exclamation-triangle" style="font-size: 32px; display: block; margin-bottom: 10px;"></i>
          ${escHtml(error.message)}
        </td>
      </tr>
    `;
    cardList.innerHTML = '<div class="family-card-empty">Failed to load product families.</div>';
    showToast(error.message, 'error');
  }
}

async function saveProductFamily() {
  const pdfName = _familyPanelState.pdf || selectedPdf || '';
  const familyId = (document.getElementById('family-id')?.value || _familySelectedId || '').trim();
  const productName = document.getElementById('family-name')?.value.trim() || '';
  const productCode = document.getElementById('family-code')?.value.trim() || '';
  const rawCategory = document.getElementById('family-category')?.value.trim() || '';
  const reviewStatus = document.getElementById('family-review-status')?.value || 'approved';
  const chunkIds = [..._familySelectedChunkIds];

  if (!pdfName) {
    showToast('Select a chunked PDF first.', 'error');
    return;
  }
  if (!productName) {
    showToast('Product family name is required.', 'error');
    return;
  }
  if (!chunkIds.length) {
    showToast('Select at least one chunk.', 'error');
    return;
  }

  const button = document.querySelector('.family-actions .btn-primary');
  if (button) {
    button.disabled = true;
    button.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Saving…';
  }

  try {
    const res = await fetch('/admin-panel/api/families/save/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({
        pdf: pdfName,
        family_id: familyId,
        product_name: productName,
        product_code: productCode,
        raw_category: rawCategory,
        review_status: reviewStatus,
        chunk_ids: chunkIds,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not save product family.');

    _familyPanelState = {
      pdf: data.pdf || pdfName,
      document_id: data.document_id || _familyPanelState.document_id,
      chunks: data.chunks || [],
      families: data.families || [],
      chunksById: Object.fromEntries((data.chunks || []).map(chunk => [String(chunk.id), chunk])),
      familiesById: Object.fromEntries((data.families || []).map(family => [String(family.id), family])),
      search: document.getElementById('family-chunk-search')?.value || '',
    };
    _familySelectedId = data.family?.id || familyId || '';
    _familySelectedChunkIds = new Set((data.family?.chunks || []).map(chunk => String(chunk.id)));

    const id = document.getElementById('family-id');
    const name = document.getElementById('family-name');
    const code = document.getElementById('family-code');
    const category = document.getElementById('family-category');
    const status = document.getElementById('family-review-status');
    if (id) id.value = _familySelectedId;
    if (name) name.value = data.family?.product_name || productName;
    if (code) code.value = data.family?.product_code || productCode;
    if (category) category.value = data.family?.raw_category || rawCategory;
    if (status) status.value = data.family?.review_status || reviewStatus;

    renderFamilyWorkspace();
    document.getElementById('next-to-index-families').style.display = 'flex';
    markStepDone('families');
    advanceTrackedStage('families', pdfName);
    refreshStats();
    loadPdfList();
    showToast(data.message || 'Product family saved.', 'success');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = '<i class="fa fa-save"></i> Save Family';
    }
  }
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

function escAttr(s) {
  return escHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Step 3 Index & Embed Dashboard Logic ──
let _cachedIndexChunks = [];
let _indexChatGlobalContext = false;
let _indexSelectedDocId = null;

async function loadIndexPanel() {
  const pdfTitleEl = document.getElementById('index-selected-pdf');
  const tableBody = document.getElementById('index-chunks-table-body');
  if (!pdfTitleEl || !tableBody) return;

  // 1. If selectedPdf is not set, try to select the first PDF from PDF list
  if (!selectedPdf || !_findPdfDetail(selectedPdf)?.chunks_count) {
    try {
      if (!_pdfDetails.length) {
        const res = await fetch('/admin-panel/api/pdfs/');
        const data = await res.json();
        _pdfDetails = data.pdf_details || [];
      }
      const pdfs = _pdfDetails || [];
      const picked = _findPdfDetail(selectedPdf) || _defaultChunkedPdf() || pdfs[0];
      if (picked) {
        selectedPdf = picked.name;
        _highlightPdfRow(selectedPdf);
      }
    } catch (e) {
      console.error('Failed to load PDF list in index panel', e);
    }
  }
  
  // Auto-detect and update progress when navigating to index panel
  if (selectedPdf) {
    try {
      const statsRes = await fetch('/admin-panel/api/stats/');
      const statsData = await statsRes.json();
      const currentStage = (statsData.pdf_progress || {})[selectedPdf];
      if (currentStage === 'chunked') {
        const splitRe2 = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
        const lookupPdf = splitRe2.test(selectedPdf)
          ? selectedPdf.replace(splitRe2, '') + '.pdf'
          : selectedPdf;
        const chunksRes = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(lookupPdf)}&index=1`);
        const chunksData = await chunksRes.json();
        if (chunksData.chunks && chunksData.chunks.some(c => c.status === 'Embedded')) {
          await fetch('/admin-panel/api/update-pdf-stage/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
            body: JSON.stringify({ filename: selectedPdf, stage: 'indexed' })
          });
          await refreshStats();
        }
      }
    } catch (e) {}
  }

  // 2. If selectedPdf is STILL not set (e.g. no PDFs exist)
  if (!selectedPdf) {
    pdfTitleEl.textContent = 'No PDF Selected';
    tableBody.innerHTML = `
      <tr>
        <td colspan="4" class="index-table-empty">
          <i class="fa fa-file-pdf" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
          No PDFs processed yet. Please upload a PDF in Step 1 and create chunks in Step 2.
        </td>
      </tr>
    `;
    updateChatContextDisplay();
    return;
  }

  // 3. Resolve the PDF name to use for chunks lookup:
  //    If selectedPdf is a split part, use the parent stem so we aggregate all parts' chunks.
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  const chunksPdf = splitRe.test(selectedPdf)
    ? selectedPdf.replace(splitRe, '') + '.pdf'
    : selectedPdf;
  const displayName = splitRe.test(selectedPdf)
    ? selectedPdf.replace(splitRe, '')
    : selectedPdf;

  pdfTitleEl.textContent = displayName;
  updateChatContextDisplay();

  tableBody.innerHTML = `
    <tr>
      <td colspan="4" class="index-table-empty">
        <i class="fa fa-spinner fa-spin" style="font-size: 24px; display: block; margin-bottom: 10px; color: var(--orange);"></i>
        Loading chunks for "${escHtml(displayName)}"...
      </td>
    </tr>
  `;

  try {
    const res = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(chunksPdf)}&index=1`);
    const data = await res.json();
    if (data.error) {
      tableBody.innerHTML = `
        <tr>
          <td colspan="4" class="index-table-empty" style="color:var(--red)">
            <i class="fa fa-exclamation-circle" style="font-size: 32px; display: block; margin-bottom: 10px;"></i>
            Error: ${escHtml(data.error)}
          </td>
        </tr>
      `;
      _cachedIndexChunks = [];
      _indexSelectedDocId = null;
    } else {
      _cachedIndexChunks = data.chunks || [];
      _indexSelectedDocId = data.document_id;
      renderIndexChunks(_cachedIndexChunks);
      // Show approve button immediately if there are embedded chunks (no query required)
      if (_cachedIndexChunks.some(c => c.status === 'Embedded')) {
        showMarkAsTestedButton();
      }
    }
  } catch (err) {
    tableBody.innerHTML = `
      <tr>
        <td colspan="4" class="index-table-empty" style="color:var(--red)">
          <i class="fa fa-exclamation-circle" style="font-size: 32px; display: block; margin-bottom: 10px;"></i>
          Failed to fetch chunks. Please check connection.
        </td>
      </tr>
    `;
    _cachedIndexChunks = [];
    _indexSelectedDocId = null;
  }
}

function renderIndexChunks(chunks) {
  const tableBody = document.getElementById('index-chunks-table-body');
  if (!tableBody) return;
  
  tableBody.innerHTML = '';
  
  if (!chunks.length) {
    tableBody.innerHTML = `
      <tr>
        <td colspan="4" class="index-table-empty">
          <i class="fa fa-search" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
          No matching chunks found.
        </td>
      </tr>
    `;
    return;
  }

  chunks.forEach(c => {
    const tr = document.createElement('tr');
    
    // Chunk ID format
    const chunkId = `C${String(c.ordinal || 0).padStart(3, '0')}`;
    
    // Excerpt snippet
    let cleanText = String(c.content || '').trim();
    cleanText = cleanText.replace(/\s+/g, ' '); // collapse spaces & newlines
    let excerpt = cleanText;
    if (cleanText.length > 90) {
      excerpt = `...${cleanText.substring(0, 90)}...`;
    } else if (cleanText.length > 0) {
      excerpt = `...${cleanText}...`;
    }

    // Status Badge HTML
    let badgeHtml = '';
    if (c.status === 'Embedded') {
      badgeHtml = `<span class="badge-status-embedded"><i class="fa fa-database"></i> Embedded</span>`;
    } else {
      badgeHtml = `<span class="badge-status-ready"><i class="fa fa-check-circle"></i> Ready</span>`;
    }

    tr.innerHTML = `
      <td style="font-weight:700; color:var(--dark); font-family:monospace;">${escHtml(chunkId)}</td>
      <td style="font-weight:600; color:#475569;">${escHtml(c.product_name || 'General Info')}</td>
      <td style="color:#64748b; font-style:italic;">${escHtml(excerpt)}</td>
      <td style="text-align:center;">${badgeHtml}</td>
    `;
    tableBody.appendChild(tr);
  });
}

function handleIndexSearch() {
  const searchInput = document.getElementById('index-chunk-search');
  if (!searchInput) return;
  const q = searchInput.value.toLowerCase().trim();
  
  if (!q) {
    renderIndexChunks(_cachedIndexChunks);
    return;
  }

  const filtered = _cachedIndexChunks.filter(c => {
    const chunkId = `C${String(c.ordinal || 0).padStart(3, '0')}`.toLowerCase();
    const prodName = (c.product_name || '').toLowerCase();
    const content = (c.content || '').toLowerCase();
    return chunkId.includes(q) || prodName.includes(q) || content.includes(q);
  });
  
  renderIndexChunks(filtered);
}

function updateChatContextDisplay() {
  const badge = document.getElementById('index-chat-context-badge');
  const btn = document.getElementById('btn-toggle-chat-context');
  if (!badge || !btn) return;

  if (_indexChatGlobalContext) {
    btn.classList.add('active');
    btn.innerHTML = `<i class="fa fa-comments"></i> Test Chat (with selected PDF context)`;
    badge.textContent = 'Global Context';
    badge.className = 'badge-context global';
  } else {
    btn.classList.remove('active');
    btn.innerHTML = `<i class="fa fa-comments"></i> Test Chat (with global context)`;
    badge.textContent = selectedPdf ? selectedPdf : 'PDF Context';
    badge.className = 'badge-context';
  }
}

function toggleChatContext() {
  _indexChatGlobalContext = !_indexChatGlobalContext;
  updateChatContextDisplay();
  
  const systemMsg = _indexChatGlobalContext 
    ? '<i>System: Changed query context to <strong>Global (All PDFs)</strong>. Queries will search across all indexed catalog data.</i>'
    : `<i>System: Changed query context to <strong>Selected PDF (${escHtml(selectedPdf)})</strong>. Queries will target only this PDF's chunks.</i>`;
    
  appendIndexChatMsg('bot', systemMsg);
}

function handleIndexChatKey(event) {
  if (event.key === 'Enter') {
    submitIndexChat();
  }
}

function appendIndexChatMsg(role, html) {
  const msgs = document.getElementById('index-chat-msgs');
  if (!msgs) return;
  const div = document.createElement('div');
  div.className = `admin-msg ${role}`;
  
  const avatarBg = role === 'bot' ? 'background:var(--orange);' : 'background:var(--dark);';
  const avatarInner = role === 'bot' ? 'GZ' : '<i class="fa fa-user"></i>';
  
  div.innerHTML = `
    <div class="admin-msg-avatar" style="${avatarBg}">${avatarInner}</div>
    <div class="admin-msg-bubble msg-content">${html}</div>
  `;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

async function submitIndexChat() {
  const input = document.getElementById('index-chat-input');
  if (!input) return;
  const query = input.value.trim();
  if (!query) return;

  appendIndexChatMsg('user', escHtml(query));
  input.value = '';

  // Append typing indicator
  const msgs = document.getElementById('index-chat-msgs');
  const typing = document.createElement('div');
  typing.id = 'index-chat-typing';
  typing.className = 'admin-msg bot';
  typing.innerHTML = `
    <div class="admin-msg-avatar" style="background:var(--orange)">GZ</div>
    <div class="admin-msg-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>
  `;
  msgs.appendChild(typing);
  msgs.scrollTop = msgs.scrollHeight;

  // Determine query scope
  let docIds = [];
  if (!_indexChatGlobalContext) {
    if (_indexSelectedDocId) {
      docIds = [_indexSelectedDocId];
    } else {
      document.getElementById('index-chat-typing')?.remove();
      appendIndexChatMsg('bot', '<span style="color:var(--orange)">Wait for chunks to be loaded, or switch to Global Context.</span>');
      return;
    }
  }

  try {
    const res = await fetch('/admin-panel/api/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ query, document_ids: docIds })
    });
    const data = await res.json();
    document.getElementById('index-chat-typing')?.remove();
    
    if (data.error) {
      appendIndexChatMsg('bot', `<span style="color:var(--orange)">${escHtml(data.error)}</span>`);
    } else {
      const src = (data.sources || []).map(s => `
        <span style="background:var(--orange-light);color:var(--orange);padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700">
          ${escHtml(s.name || s.code)}
        </span>
      `).join(' ');
      
      const answerHtml = renderMarkdown(data.answer);
      const sourcesBlock = src ? `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">${src}</div>` : '';
      
      appendIndexChatMsg('bot', answerHtml + sourcesBlock);
      
      // Show "Mark as Tested" button if not already tested
      showMarkAsTestedButton();
    }
  } catch (err) {
    document.getElementById('index-chat-typing')?.remove();
    appendIndexChatMsg('bot', '<span style="color:var(--orange)">Network error. Please try again.</span>');
  }
}

function showMarkAsTestedButton() {
  if (!selectedPdf) return;

  const chatInputRow = document.querySelector('.index-chat-input-row');
  if (!chatInputRow) return;

  let buttonContainer = document.getElementById('approval-button-container');
  if (!buttonContainer) {
    buttonContainer = document.createElement('div');
    buttonContainer.id = 'approval-button-container';
    buttonContainer.style.cssText = 'padding: 12px 16px 0 16px; display: flex; flex-direction: column; gap: 8px;';
    chatInputRow.parentElement.appendChild(buttonContainer);
  }
  buttonContainer.innerHTML = ''; // clear and re-render

  const alreadyTested = _trackedStage === 'tested';

  // Approve button — always present, disabled+green when already approved
  const approveBtn = document.createElement('button');
  approveBtn.id = 'mark-tested-btn';
  approveBtn.className = 'btn btn-success';
  approveBtn.style.cssText = 'width: 100%;';
  if (alreadyTested) {
    approveBtn.disabled = true;
    approveBtn.style.cssText = 'width: 100%; background: #22c55e; color: white; font-weight: 600; opacity: 1; cursor: not-allowed;';
    approveBtn.innerHTML = '<i class="fa fa-check-double"></i> Approved! Testing Complete';
  } else {
    approveBtn.innerHTML = '<i class="fa fa-check-circle"></i> Approve Testing (Mark as 100%)';
    approveBtn.onclick = markPdfAsTested;
  }
  buttonContainer.appendChild(approveBtn);

  // Disapprove button — only shown when already approved
  if (alreadyTested) {
    const disapproveBtn = document.createElement('button');
    disapproveBtn.id = 'disapprove-tested-btn';
    disapproveBtn.style.cssText = 'width: 100%; background: transparent; border: 1px solid #9ca3af; color: #6b7280; font-size: 12px; padding: 6px; border-radius: 6px; cursor: pointer;';
    disapproveBtn.innerHTML = '<i class="fa fa-undo"></i> Disapprove Testing';
    disapproveBtn.onclick = disapproveTesting;
    buttonContainer.appendChild(disapproveBtn);
  }
}

async function markPdfAsTested() {
  const pdfTitleEl = document.getElementById('index-selected-pdf');
  const pdfName = selectedPdf || (pdfTitleEl ? pdfTitleEl.textContent.trim() : '');
  const displayPdf = pdfTitleEl ? pdfTitleEl.textContent.trim() : pdfName;

  if (!pdfName || pdfName === 'No PDF Selected') {
    showToast('No PDF selected for testing approval', 'error');
    return;
  }

  const btn = document.getElementById('mark-tested-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Approving...';

  try {
    const res = await fetch('/admin-panel/api/update-pdf-stage/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: pdfName, stage: 'tested' })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to update stage');

    const _display = _trackedPdf || displayPdf || pdfName.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || pdfName;
    _trackedPdf   = _display;
    _trackedStage = 'tested';
    _trackedSetAt = Date.now();
    _userSelectedPdf = null;
    _renderProgress(_display, 'tested');
    showToast(`"${pdfName}" testing approved! Progress updated to 100%`, 'success');
    showMarkAsTestedButton(); // re-render: disable approve + show disapprove button
  } catch (error) {
    showToast(error.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-check-circle"></i> Approve Testing (Mark as 100%)';
  }
}

async function disapproveTesting() {
  const pdfTitleEl = document.getElementById('index-selected-pdf');
  const pdfName = selectedPdf || (pdfTitleEl ? pdfTitleEl.textContent.trim() : '');
  if (!pdfName || pdfName === 'No PDF Selected') return;

  const btn = document.getElementById('mark-tested-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Disapproving...';

  try {
    const res = await fetch('/admin-panel/api/update-pdf-stage/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: pdfName, stage: 'indexed' })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to update stage');

    const _display = _trackedPdf || pdfName.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || pdfName;
    _trackedPdf   = _display;
    _trackedStage = 'indexed';
    _trackedSetAt = Date.now();
    _userSelectedPdf = null;
    _renderProgress(_display, 'indexed');
    showToast(`"${pdfName}" testing disapproved. Progress reverted to 85%.`, 'info');

    // Re-render buttons: show clickable Approve button, hide Disapprove
    showMarkAsTestedButton();
  } catch (error) {
    showToast(error.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-times-circle"></i> Disapprove Testing';
  }
}
