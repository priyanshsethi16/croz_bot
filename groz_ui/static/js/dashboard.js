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
  const text = String(errText).trim();
  const lowerText = text.toLowerCase();
  
  // Check for common API errors
  if (lowerText.includes('high demand') || lowerText.includes('experiencing high demand')) {
    return 'Google Gemini is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.';
  }
  if (lowerText.includes('503 unavailable') || lowerText.includes('503 service unavailable')) {
    return 'Google Gemini API is temporarily unavailable (503). Please try again in a few minutes.';
  }
  if (lowerText.includes('quota exceeded') || lowerText.includes('resource_exhausted') || lowerText.includes('429')) {
    return 'API quota limit exceeded. Please wait a moment before trying again.';
  }
  if (lowerText.includes('gemini_api_key is required') || lowerText.includes('api key not valid')) {
    return 'Gemini API Key is missing or invalid. Please configure it in Models & Keys.';
  }
  if (lowerText.includes('[failed] all retries exhausted')) {
    return 'Gemini extraction failed after all retries for one or more pages.';
  }

  const isNoiseLine = (line) => {
    const trimmed = String(line || '').trim();
    if (!trimmed) return true;
    if (/^[\^~`\-|. ]+$/.test(trimmed)) return true;
    if (trimmed.startsWith('Traceback (most recent call last):')) return true;
    if (trimmed.startsWith('During handling of the above exception')) return true;
    if (trimmed.startsWith('The above exception was the direct cause')) return true;
    if (trimmed.startsWith('File "')) return true;
    if ((/\d+%\|/.test(trimmed) && trimmed.includes('[') && trimmed.includes(']')) || trimmed.includes('it/s]')) return true;
    if (/^(return|raise|await|for|if|elif|else|with|def|class)\b/.test(trimmed)) return true;
    return false;
  };

  const errorLinePatterns = [
    /^(?:[A-Za-z_][\w.]*\.)?(?:[A-Za-z_][\w]*(?:Error|Exception)|InvalidArgument|ResourceExhausted|ServiceUnavailable|TooManyRequests|DeadlineExceeded|PermissionDenied|Unauthenticated|FailedPrecondition|NotFound|Aborted|RuntimeError|ValueError|SyntaxError):\s*(.+)$/i,
    /^ERROR:\s*(.+)$/i,
    /All Gemini (?:keys|retries) failed:\s*(.+)$/i,
  ];

  // Fallback: extract the last non-empty line of the error text
  const lines = text.replace(/\r/g, '\n').split('\n').map(l => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (isNoiseLine(line)) continue;
    for (const pattern of errorLinePatterns) {
      const match = line.match(pattern);
      if (match && match[1] && match[1].trim().toLowerCase() !== 'none') {
        return match[1].trim();
      }
    }
    if (line.toUpperCase().startsWith('WARNING:')) {
      const warningMessage = line.replace(/^WARNING:\s*/i, '').trim();
      if (/\b(failed|error)\b/i.test(warningMessage)) {
        return warningMessage.slice(0, 500);
      }
    }
  }
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!isNoiseLine(lines[i])) return lines[i];
  }
  return 'Pipeline failed. Check the server logs for the full error.';
}

// ── Panel navigation ──────────────────────────────────────────────────────────
let _pdfDetails = [];  // shared cache — populated by loadPdfList, reused by loadExistingUploads
let _pdfDetailsPromise = null;
let _userSelectedPdf = null; // PDF explicitly selected by user clicking a row
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

async function ensurePdfDetails() {
  if (_pdfDetails && _pdfDetails.length > 0) return _pdfDetails;
  if (_pdfDetailsPromise) return _pdfDetailsPromise;

  _pdfDetailsPromise = (async () => {
    const res = await fetch('/admin-panel/api/pdfs/');
    const data = await res.json();
    _pdfDetails = data.pdf_details || [];
    return _pdfDetails;
  })();

  try {
    return await _pdfDetailsPromise;
  } finally {
    _pdfDetailsPromise = null;
  }
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
  selectedPdf = parentStem;
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
  await ensurePdfDetails();
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
        const percent = 20 + (doneCount / totalCount) * 20;
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

  renderSplitParts(splits, true);

  // Load the full parent PDF pages in the viewer
  const strip  = document.getElementById('pdf-thumb-strip');
  const viewer = document.getElementById('pdf-page-viewer');
  const countEl = document.getElementById('preview-page-count');
  const subEl   = document.getElementById('preview-sub');
  strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-spinner fa-spin"></i> Rendering pages…</div>';
  viewer.innerHTML = '';
  try {
    const parentPdfName = parentStem + '.pdf';
    // Try parent PDF first; if it doesn't exist (was re-split), use first child part
    let previewUrl = '/admin-panel/api/pdf-preview/?pdf=' + encodeURIComponent(parentPdfName);
    let res  = await fetch(previewUrl);
    let data = await res.json();
    if (data.error && splits.length > 0) {
      // Parent PDF not on disk — load first child part instead
      const firstPart = splits[0];
      previewUrl = `/admin-panel/api/pdf-preview/?stem=${encodeURIComponent(parentStem)}&part=${encodeURIComponent(firstPart.filename)}`;
      res  = await fetch(previewUrl);
      data = await res.json();
    }
    if (!data.error) {
      countEl.textContent = `${data.total} pages`;
      subEl.textContent   = `${parts.length} split parts · ${data.total} pages total`;
      window._previewPages    = data.pages;
      window._previewFilename = parentStem + '.pdf';
      window._previewTotal    = data.total;
      window._previewLoaded   = data.pages.length;
      strip.innerHTML = data.pages.map((p, i) => `
        <div class="pdf-thumb-item${i === 0 ? ' active' : ''}" onclick="selectPreviewPage(${i})" id="thumb-${i}">
          <img src="${p.thumb}" alt="Page ${p.num}" loading="lazy">
          <span>${p.num}</span>
        </div>`).join('');
      if (data.pages.length) renderContinuousPreview(data.pages);
    } else {
      strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Could not load parent PDF preview.</div>';
    }
  } catch(e) {
    strip.innerHTML = '<div class="pdf-preview-loading"><i class="fa fa-exclamation-triangle"></i> Failed to load preview.</div>';
  }
}

async function loadExistingUploads() {
  const list = document.getElementById('upload-file-list');
  if (!list) return;
  list.innerHTML = `
    <div class="file-item" style="justify-content:center;color:var(--grey);font-size:13px;gap:10px">
      <i class="fa fa-spinner fa-spin"></i>
      <span>Loading uploaded PDFs…</span>
    </div>`;
  try {
    await ensurePdfDetails();
    const pdfs = _pdfDetails.map(d => d.name);

    // Separate plain PDFs from split parts
    const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
    const groups  = {};  // parentStem -> [splitName, ...]
    const plain   = [];

    pdfs.forEach(name => {
      if (splitRe.test(name)) {
        const root = _rootStem(name);
        (groups[root] = groups[root] || []).push(name);
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

      // Compute parent status from children chunk counts
      const childDetails = parts.map(p => _pdfDetails.find(d => d.name === p));
      const anyChunked = childDetails.some(d => d && d.chunks_count > 0);
      const allChunked = childDetails.length > 0 && childDetails.every(d => d && d.chunks_count > 0);
      const parentStatusCls = allChunked ? 'success' : anyChunked ? 'partial' : 'success';
      const parentStatusText = allChunked ? '✓ Chunked' : anyChunked ? `✓ ${childDetails.filter(d => d && d.chunks_count > 0).length}/${parts.length} Chunked` : '✓ Uploaded';

      // Parent row — same structure as .file-item
      const header = document.createElement('div');
      header.className = 'file-item';
      header.innerHTML = `
        <div class="file-item-icon"><i class="fa fa-file-pdf"></i></div>
        <div class="file-item-info">
          <div class="fname">${escHtml(parentStem)}</div>
          <div class="fsize">${parts.length} split part${parts.length !== 1 ? 's' : ''}</div>
        </div>
        <span class="fstatus ${parentStatusCls}">${parentStatusText}</span>
        <button class="btn btn-sm btn-secondary file-preview-btn" onclick="loadPdfPreview('${escHtml(parentStem + '.pdf')}')" title="Preview pages">
          <i class="fa fa-eye"></i> Preview
        </button>
        <button class="btn btn-sm btn-danger file-delete-btn" onclick="deleteUploadedPdf('${escHtml(parentStem + '.pdf')}', this)" title="Delete PDF">
          <i class="fa fa-trash"></i>
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
        const detail = _pdfDetails.find(d => d.name === partName);
        const isChunked = detail && detail.chunks_count > 0;
        const childStatusCls = isChunked ? 'success' : 'success';
        const childStatusText = isChunked ? '✓ Chunked' : '✓ Uploaded';
        const child = document.createElement('div');
        child.className = 'file-item';
        child.innerHTML = `
          <div class="file-item-icon"><i class="fa fa-file-pdf"></i></div>
          <div class="file-item-info"><div class="fname">${escHtml(partName)}</div></div>
          <span class="fstatus ${childStatusCls}">${childStatusText}</span>
          <button class="btn btn-sm btn-secondary file-preview-btn" onclick="loadPdfPreview('${escHtml(partName)}')" title="Preview pages">
            <i class="fa fa-eye"></i> Preview
          </button>
          <button class="btn btn-sm btn-danger file-delete-btn" onclick="deleteUploadedPdf('${escHtml(partName)}', this)" title="Delete PDF">
            <i class="fa fa-trash"></i>
          </button>`;
        children.appendChild(child);
      });

      group.appendChild(header);
      group.appendChild(children);
      list.appendChild(group);
    });
    _renderUploadPage();
  } catch(e) {
    console.error('Failed to load uploaded PDFs:', e);
    list.innerHTML = `
      <div class="file-item" style="justify-content:center;color:#dc2626;font-size:13px;gap:10px">
        <i class="fa fa-exclamation-triangle"></i>
        <span>Could not load uploaded PDFs.</span>
      </div>`;
  }
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
  // V2 structured flow: register the PDF, then poll the durable ingestion job.
  const fd = new FormData();
  fd.append('pdf', file);
  fd.append('csrfmiddlewaretoken', CSRF);
  fd.append('catalog_name', file.name.replace(/\.pdf$/i,'').replace(/[_-]+/g,' '));
  fd.append('source_type', 'catalog');
  try {
    const endpoint = '/admin-panel/api/v2/upload/';
    const res  = await fetch(endpoint, { method: 'POST', body: fd });
    const data = await res.json();
    if (res.status === 409 || data.error) {
      item.remove();
      showToast(data.error, 'error');
      return;
    } else {
      setFileStatus(item, 'success', '✓ Queued');
      showToast(data.message || 'PDF registered successfully!', 'success');
      loadPdfPreview(file.name);
      if (data.job_id) {
        pollV2IngestionJob(item, data.job_id);
      }
      refreshStats();
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
      <i class="fa fa-trash"></i>
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
  if (!await showConfirm('Delete PDF', `Delete "${filename}" and all its associated data? This cannot be undone.`)) return;
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
      btn.innerHTML = '<i class="fa fa-trash"></i>';
      showToast(data.error || 'Delete failed.', 'error');
    }
  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-trash"></i>';
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
  // If this is a parent PDF with existing split parts, show the split parts list
  await ensurePdfDetails();
  const stem = filename.replace(/\.pdf$/i, '');
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  const splitChildren = _pdfDetails.filter(d => splitRe.test(d.name) && d.name.replace(splitRe, '') === stem);
  if (splitChildren.length > 0) {
    await previewParentGroup(stem, splitChildren.map(d => d.name));
    return;
  }

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
      // Only set the tracked PDF if it's not already at a higher stage
      const serverStage = approveData.stage || 'uploaded';
      const STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested'];
      const currentStageIdx = _trackedStage ? STAGE_ORDER.indexOf(_trackedStage) : -1;
      const serverStageIdx = STAGE_ORDER.indexOf(serverStage);
      
      // Only update if server stage is higher, or if we have no current stage
      if (currentStageIdx === -1 || serverStageIdx > currentStageIdx) {
        setTrackedPdf(filename, serverStage);
      }
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
    subEl.textContent   = `${data.total} page${data.total !== 1 ? 's' : ''}`;

    window._previewPages    = data.pages;
    window._previewFilename = filename;
    window._previewTotal    = data.total;
    window._previewLoaded   = data.pages.length;

    strip.innerHTML = data.pages.map((p, i) => `
      <div class="pdf-thumb-item${i === 0 ? ' active' : ''}" onclick="selectPreviewPage(${i})" id="thumb-${i}">
        <img src="${p.thumb}" alt="Page ${p.num}" loading="lazy"/>
        <span class="thumb-num">${p.num}</span>
      </div>`).join('');

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
let _pipelineRunning = false;

function _rootStem(name) {
  // Repeatedly strip split suffixes to find the ultimate root stem
  const splitRe = /_(custom_)?p\d{4}-\d{4}(\.pdf)?$/i;
  let stem = name.replace(/\.pdf$/i, '');
  while (splitRe.test(stem)) {
    stem = stem.replace(splitRe, '');
  }
  return stem;
}

function _chunkGroupRows(pdfDetails) {
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  const groups  = {};  // rootStem -> [item, ...]
  const plain   = [];
  pdfDetails.forEach(item => {
    if (splitRe.test(item.name)) {
      const root = _rootStem(item.name);
      (groups[root] = groups[root] || []).push(item);
    } else {
      plain.push(item);
    }
  });
  const rows = [];
  plain.forEach(item => {
    const stem = item.name.replace(/\.pdf$/i, '');
    if (groups[stem]) {
      rows.push({ type: 'group', stem, children: groups[stem] });
      delete groups[stem];
    } else {
      rows.push({ type: 'plain', item });
    }
  });
  Object.entries(groups).forEach(([stem, children]) => {
    rows.push({ type: 'group', stem, children });
  });
  return rows;
}

function _toggleChunkGroup(stem) {
  const btn = document.getElementById('ckg-btn-' + stem);
  const children = document.querySelectorAll('.ckg-child-' + CSS.escape(stem));
  const isOpen = btn && btn.classList.contains('open');
  const nextOpen = !isOpen;
  if (btn) {
    btn.classList.toggle('open', nextOpen);
    const icon = btn.querySelector('i');
    if (icon) icon.className = 'fa fa-chevron-' + (nextOpen ? 'down' : 'right');
  }
  // Chunk rows are also managed by the panel paginator, so make the inline
  // display state explicit when expanding/collapsing the group.
  children.forEach(r => {
    r.classList.toggle('visible', nextOpen);
    r.style.display = nextOpen ? 'table-row' : 'none';
  });
}

function _buildChunkRowActions(item) {
  let a = '<div class="table-actions">';
  const dis = item.chunks_count === 0 ? 'disabled' : '';
  a += `<button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(item.name)}')" ${dis} title="View chunks"><i class="fa fa-layer-group"></i> Chunks</button>`;
  if (IS_ADMIN && item.chunks_count > 0 && item.document_id) {
    a += `<button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(item.name)})' title="Families"><i class="fa fa-sitemap"></i> Families</button>`;
  }
  if (item.status === 'Ready' || item.chunks_count > 0) {
    if (item.has_embeddings) {
      a += `<button class="btn btn-sm embed-btn" disabled style="background:#22c55e;color:#fff;opacity:1;cursor:not-allowed"><i class="fa fa-check-circle"></i> Embedded</button>`;
    } else if (item.document_id) {
      a += `<button class="btn btn-sm btn-primary embed-btn" onclick="triggerEmbeddingInline('${item.document_id}','${escHtml(item.name)}')"><i class="fa fa-brain"></i> Create Embedding</button>`;
    } else {
      a += `<button class="btn btn-sm btn-primary embed-btn" disabled><i class="fa fa-brain"></i> Create Embedding</button>`;
    }
  } else if (item.status === 'Processing') {
    a += `<button class="btn btn-sm btn-primary embed-btn" disabled><i class="fa fa-spinner fa-spin"></i> Processing</button>`;
  } else {
    a += `<button class="btn btn-sm btn-primary" onclick="runChunkingForPdf('${escHtml(item.name)}',this)"><i class="fa fa-layer-group"></i> Create Chunks</button>`;
  }
  a += `<button class="btn btn-sm btn-danger" onclick="deleteEmbeddingsOnly('${escHtml(item.name)}')"><i class="fa fa-trash"></i></button>`;
  a += `<button class="btn btn-sm btn-secondary" onclick="openUploadPreview('${escHtml(item.name)}')" title="View PDF preview"><i class="fa fa-eye"></i></button>`;
  a += '</div>';
  return a;
}

function _chunkPanelDisplayStatus(item) {
  const rawStatus = String(item?.status || '').toLowerCase();
  if (rawStatus === 'failed') return 'Failed';
  if (rawStatus === 'processing') return 'Processing';
  if (item?.has_embeddings) return 'Ready';
  return 'Pending';
}

function _chunkPanelGroupStatus(children = []) {
  const normalized = children.map(child => _chunkPanelDisplayStatus(child));
  if (normalized.includes('Processing')) return 'Processing';
  if (children.length === 0) return 'Pending';
  if (normalized.every(status => status === 'Ready')) return 'Ready';
  if (normalized.every(status => status === 'Failed')) return 'Failed';
  if (normalized.includes('Failed') || normalized.includes('Ready')) return 'Partial';
  return 'Pending';
}

function _chunkPanelStatusBadge(status, compact = false) {
  const iconSize = compact ? ' style="font-size:10px"' : '';
  const label = escHtml(status || 'Pending');
  if (status === 'Processing') {
    return `<span class="badge badge-yellow"${iconSize}><i class="fa fa-spinner fa-spin"></i> Processing</span>`;
  }
  if (status === 'Partial') {
    return `<span class="badge badge-yellow"${iconSize}><i class="fa fa-adjust"></i> Partial</span>`;
  }
  if (status === 'Ready') {
    return `<span class="badge badge-green"${iconSize}><i class="fa fa-check-circle"></i> Ready</span>`;
  }
  if (status === 'Failed') {
    return `<span class="badge badge-red"${iconSize}><i class="fa fa-times-circle"></i> Failed</span>`;
  }
  return `<span class="badge badge-grey"${iconSize}><i class="fa fa-clock"></i> ${label}</span>`;
}

function _chunkPanelReviewStatus(item) {
  const rawStatus = String(item?.review_status || '').toLowerCase();
  if (rawStatus === 'approved' || rawStatus === 'needs_review') return rawStatus;

  const approvedCount = Number(item?.approved_families_count ?? item?.product_families_count ?? 0);
  const needsReviewCount = Number(item?.needs_review_families_count ?? 0);
  if (needsReviewCount > 0) return 'needs_review';
  if (approvedCount > 0) return 'approved';
  return Number(item?.chunks_count || 0) > 0 ? 'needs_review' : 'pending';
}

function _chunkPanelGroupReviewStatus(children = []) {
  const statuses = children.map(child => _chunkPanelReviewStatus(child));
  if (statuses.includes('needs_review')) return 'needs_review';
  if (statuses.includes('approved')) return 'approved';
  return 'pending';
}

async function loadPdfList() {
  const tbody = document.getElementById('pdf-selector-table-body');
  if (!tbody) return;
  try {
    await ensurePdfDetails();
    tbody.innerHTML = '';

    const pdfDetails = _pdfDetails;
    if (!pdfDetails.length) {
      tbody.innerHTML = `<tr><td colspan="5" style="color:var(--grey);font-size:13px;text-align:center;padding:24px 0">No PDFs uploaded yet. <button class="btn btn-sm btn-primary" onclick="showPanel('upload')">Upload one →</button></td></tr>`;
      return;
    }

    _chunkGroupRows(pdfDetails).forEach(row => {
      if (row.type === 'plain') {
        const item = row.item;
        const rowStatus = _chunkPanelDisplayStatus(item);
        const rowReviewStatus = _chunkPanelReviewStatus(item);
        const tr = document.createElement('tr');
        tr.dataset.pdf = item.name;
        tr.dataset.status = rowStatus.toLowerCase();
        tr.dataset.reviewStatus = rowReviewStatus.toLowerCase();
        tr.style.cursor = 'pointer';
        if (selectedPdf === item.name) tr.classList.add('selected');
        tr.addEventListener('click', (e) => { if (e.target.closest('button,a,i')) return; selectPdfRow(tr, item.name); });
        tr.innerHTML = `<td><div class="pdf-name-cell" style="display:flex;align-items:center;gap:8px"><i class="fa fa-file-pdf" style="color:#ef4444;font-size:16px"></i><span class="pname" style="font-weight:500;font-size:13px">${escHtml(item.name)}</span></div></td><td><span class="badge badge-blue">${item.chunks_count}</span></td><td><span class="badge ${(item.product_families_count||0)>0?'badge-green':'badge-grey'}">${item.product_families_count||0}</span></td><td>${_chunkPanelStatusBadge(rowStatus)}</td><td>${_buildChunkRowActions(item)}</td>`;
        tbody.appendChild(tr);

      } else {
        const { stem, children } = row;
        const totalChunks = children.reduce((s, c) => s + (c.chunks_count || 0), 0);
        const anyChunks = children.some(c => c.chunks_count > 0);
        const rowStatus = _chunkPanelGroupStatus(children);
        const rowReviewStatus = _chunkPanelGroupReviewStatus(children);
        const allEmbedded = anyChunks && children.filter(c => c.chunks_count > 0).every(c => c.has_embeddings === true);
        const childDocIds = children.map(c => c.document_id).filter(Boolean);
        let pa = '<div class="table-actions">';
        pa += `<button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(stem)}')" ${!anyChunks ? 'disabled' : ''}><i class="fa fa-layer-group"></i> Chunks</button>`;
        if (IS_ADMIN && anyChunks) pa += `<button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(stem)})'><i class="fa fa-sitemap"></i> Families</button>`;
        if (IS_ADMIN && anyChunks) {
          if (allEmbedded) {
            pa += `<button class="btn btn-sm embed-btn" id="group-embed-btn-${escHtml(stem)}" disabled style="background:#22c55e;color:#fff;opacity:1;cursor:not-allowed"><i class="fa fa-check-circle"></i> Embedded</button>`;
          } else {
            pa += `<button class="btn btn-sm btn-primary embed-btn" id="group-embed-btn-${escHtml(stem)}" onclick="triggerGroupEmbedding('${escAttr(stem)}', JSON.parse('${escAttr(JSON.stringify(childDocIds))}'), this)"><i class="fa fa-brain"></i> Create Embedding</button>`;
          }
        }
        pa += `<button class="btn btn-sm btn-danger" onclick="deleteEmbeddingsOnly('${escHtml(stem)}')"><i class="fa fa-trash"></i></button>`;
        pa += `<button class="btn btn-sm btn-secondary" onclick="openUploadPreview('${escHtml(stem + '.pdf')}')" title="View PDF preview"><i class="fa fa-eye"></i></button></div>`;

        const parentTr = document.createElement('tr');
        parentTr.className = 'pdf-group-parent';
        parentTr.dataset.pdf = stem;
        parentTr.dataset.status = rowStatus.toLowerCase();
        parentTr.dataset.reviewStatus = rowReviewStatus.toLowerCase();
        parentTr.dataset.groupStem = stem;
        parentTr.style.cursor = 'pointer';
        if (selectedPdf === stem || selectedPdf === stem + '.pdf') parentTr.classList.add('selected');
        parentTr.addEventListener('click', (e) => {
          if (e.target.closest('button,a,i')) return;
          selectPdfRow(parentTr, stem);
          // Load split parts preview for parent group row
          const partNames = children.map(c => c.name);
          if (partNames.length) previewParentGroup(stem, partNames);
        });
        const totalProducts = children.reduce((s, c) => s + (c.product_families_count || 0), 0);
        parentTr.innerHTML = `<td><div class="pdf-name-cell" style="display:flex;align-items:center;gap:6px"><i class="fa fa-file-pdf" style="color:#ef4444;font-size:16px"></i><span class="pname" style="font-weight:600;font-size:13px">${escHtml(stem)}</span><span class="split-count-badge">${children.length} parts</span></div></td><td><span class="badge badge-blue">${totalChunks}</span></td><td><span class="badge ${totalProducts>0?'badge-green':'badge-grey'}">${totalProducts}</span></td><td>${_chunkPanelStatusBadge(rowStatus)}</td><td><div class="table-actions">${pa}<button class="pdf-group-expand-btn" id="ckg-btn-${escHtml(stem)}" onclick="event.stopPropagation();_toggleChunkGroup('${escHtml(stem)}')" title="Show split parts"><i class="fa fa-chevron-right"></i></button></div></td>`;
        tbody.appendChild(parentTr);

        children.forEach(child => {
          const childStatus = _chunkPanelDisplayStatus(child);
          const childReviewStatus = _chunkPanelReviewStatus(child);
          const childTr = document.createElement('tr');
          childTr.className = `pdf-child-row ckg-child-${escHtml(stem)}`;
          childTr.dataset.pdf = child.name;
          childTr.dataset.status = childStatus.toLowerCase();
          childTr.dataset.reviewStatus = childReviewStatus.toLowerCase();
          childTr.dataset.groupStem = stem;
          if (selectedPdf === child.name) childTr.classList.add('selected');
          childTr.addEventListener('click', (e) => { if (e.target.closest('button,a,i')) return; selectPdfRow(childTr, child.name); });
          childTr.innerHTML = `<td><div class="pdf-name-cell" style="display:flex;align-items:center;gap:8px"><i class="fa fa-file-pdf" style="color:#f97316;font-size:13px"></i><span style="font-size:12px;color:#555;font-weight:500">${escHtml(child.name)}</span></div></td><td><span class="badge badge-blue" style="font-size:10px">${child.chunks_count}</span></td><td><span class="badge ${(child.product_families_count||0)>0?'badge-green':'badge-grey'} " style="font-size:10px">${child.product_families_count||0}</span></td><td>${_chunkPanelStatusBadge(childStatus, true)}</td><td>${_buildChunkRowActions(child)}</td>`;
          tbody.appendChild(childTr);
        });
      }
    });

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
    _renderChunkPanelPage();

  } catch(e) {
    console.error('Error loading PDF list: ', e);
  }
}

let selectedPdf = null;

// ── Chunk panel search + pagination ──────────────────────────────────────────
const _chunkPanelState = { page: 1, pageSize: 15, query: '', status: 'all', reviewStatus: 'all' };

function _chunkPanelFilter() {
  const q = (document.getElementById('chunk-panel-search')?.value || '').toLowerCase().trim();
  const status = (document.getElementById('chunk-panel-status')?.value || 'all').toLowerCase().trim();
  const reviewStatus = (document.getElementById('chunk-panel-review-status')?.value || 'all').toLowerCase().trim();
  if (q !== _chunkPanelState.query || status !== _chunkPanelState.status || reviewStatus !== _chunkPanelState.reviewStatus) _chunkPanelState.page = 1;
  _chunkPanelState.query = q;
  _chunkPanelState.status = status;
  _chunkPanelState.reviewStatus = reviewStatus;
  _renderChunkPanelPage();
}

function _chunkPanelSetPageSize(n) {
  _chunkPanelState.pageSize = n;
  _chunkPanelState.page = 1;
  _renderChunkPanelPage();
}

function _renderChunkPanelPage() {
  const tbody = document.getElementById('pdf-selector-table-body');
  if (!tbody) return;
  tbody.querySelector('.chunk-panel-empty-row')?.remove();
  const q = _chunkPanelState.query;
  const statusFilter = _chunkPanelState.status || 'all';
  const reviewFilter = _chunkPanelState.reviewStatus || 'all';
  const allRows = Array.from(tbody.querySelectorAll('tr:not(.ckg-child-hidden-placeholder)'));
  const topRows = allRows.filter(r => !r.className.startsWith('pdf-child-row'));
  const filtered = topRows.filter(r => {
    const matchesSearch = q ? (r.textContent || '').toLowerCase().includes(q) : true;
    const rowStatus = (r.dataset.status || 'pending').toLowerCase();
    const rowReviewStatus = (r.dataset.reviewStatus || 'pending').toLowerCase();
    const matchesStatus = statusFilter === 'all' ? true : rowStatus === statusFilter;
    const matchesReview = reviewFilter === 'all' ? true : rowReviewStatus === reviewFilter;
    return matchesSearch && matchesStatus && matchesReview;
  });
  const pageSize = _chunkPanelState.pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  _chunkPanelState.page = Math.min(_chunkPanelState.page, totalPages);
  const start = (_chunkPanelState.page - 1) * pageSize;
  const pageRows = new Set(filtered.slice(start, start + pageSize));
  const visibleGroupStems = new Set(
    [...pageRows]
      .map(r => r.dataset.groupStem)
      .filter(Boolean)
  );
  allRows.forEach(r => {
    if (r.className.startsWith('pdf-child-row')) {
      const stem = r.dataset.groupStem || '';
      const parentVisible = visibleGroupStems.has(stem);
      r.style.display = parentVisible && r.classList.contains('visible') ? 'table-row' : 'none';
      return;
    }
    r.style.display = pageRows.has(r) ? '' : 'none';
  });
  if (!filtered.length) {
    const emptyRow = document.createElement('tr');
    emptyRow.className = 'chunk-panel-empty-row';
    emptyRow.innerHTML = `
      <td colspan="5" class="index-table-empty">
        <i class="fa fa-search" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
        No matching PDFs found.
      </td>
    `;
    tbody.appendChild(emptyRow);
  }
  const countEl = document.getElementById('chunk-panel-count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} PDF${filtered.length !== 1 ? 's' : ''}` : 'No results';
  _renderPagination('chunk-panel-pagination', _chunkPanelState.page, totalPages,
    p => { _chunkPanelState.page = p; _renderChunkPanelPage(); },
    _chunkPanelState.pageSize, '_chunkPanelSetPageSize');
}

// ── Upload panel search + pagination ───────────────────────────────────────────────────
const _uploadPanelState = { page: 1, pageSize: 15, query: '' };
let _uploadAllItems = [];  // flat list of {el, name} for pagination

function _uploadPanelSetPageSize(n) {
  _uploadPanelState.pageSize = n;
  _uploadPanelState.page = 1;
  _renderUploadPage();
}

function _uploadPanelFilter() {
  const q = (document.getElementById('upload-panel-search')?.value || '').toLowerCase().trim();
  if (q !== _uploadPanelState.query) _uploadPanelState.page = 1;
  _uploadPanelState.query = q;
  _renderUploadPage();
}

function _renderUploadPage() {
  const list = document.getElementById('upload-file-list');
  if (!list) return;
  const q = _uploadPanelState.query;
  // Top-level items: .file-item (plain) and .file-group (split group)
  const topItems = Array.from(list.children).filter(el =>
    el.classList.contains('file-item') || el.classList.contains('file-group')
  );
  const filtered = q ? topItems.filter(el => (el.textContent || '').toLowerCase().includes(q)) : topItems;
  const pageSize = _uploadPanelState.pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  _uploadPanelState.page = Math.min(_uploadPanelState.page, totalPages);
  const start = (_uploadPanelState.page - 1) * pageSize;
  const pageSet = new Set(filtered.slice(start, start + pageSize));
  topItems.forEach(el => { el.style.display = pageSet.has(el) ? '' : 'none'; });
  const countEl = document.getElementById('upload-panel-count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} file${filtered.length !== 1 ? 's' : ''}` : 'No results';
  _renderPagination('upload-panel-pagination', _uploadPanelState.page, totalPages,
    p => { _uploadPanelState.page = p; _renderUploadPage(); },
    _uploadPanelState.pageSize, '_uploadPanelSetPageSize');
}

// ── Shared pagination renderer ────────────────────────────────────────────────
function _renderPagination(containerId, currentPage, totalPages, onPageClick, currentPageSize, pageSizeFn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (totalPages <= 1 && !pageSizeFn) { el.innerHTML = ''; return; }

  const pageSizes = [10, 20, 50, 100];
  let sizeHtml = '';
  if (pageSizeFn) {
    sizeHtml = `<div style="display:flex;align-items:center;gap:6px;margin-left:auto">
      <span style="font-size:12px;color:#888;white-space:nowrap">Per page:</span>
      <div style="display:flex;gap:3px">`;
    pageSizes.forEach(s => {
      const active = s === currentPageSize;
      sizeHtml += `<button onclick="${pageSizeFn}(${s})" style="padding:3px 8px;border:1px solid ${active ? 'var(--orange)' : '#e2e8f0'};border-radius:4px;background:${active ? 'var(--orange)' : '#fff'};color:${active ? '#fff' : '#374151'};font-size:11px;cursor:${active ? 'default' : 'pointer'};font-weight:${active ? '600' : '400'}" ${active ? 'disabled' : ''}>${s}</button>`;
    });
    sizeHtml += `</div></div>`;
  }

  if (totalPages <= 1) {
    el.innerHTML = `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:6px 0">${sizeHtml}</div>`;
    return;
  }

  const btnBase = 'padding:4px 10px;border-radius:5px;font-size:12px;cursor:pointer;transition:all 0.15s;';
  const btnActive = btnBase + 'border:1px solid var(--orange);background:var(--orange);color:#fff;font-weight:600;cursor:default;';
  const btnNormal = btnBase + 'border:1px solid #e2e8f0;background:#fff;color:#374151;font-weight:400;';
  const btnDisabled = btnBase + 'border:1px solid #e2e8f0;background:#f8fafc;color:#cbd5e1;cursor:not-allowed;';

  let pagesHtml = `<button style="${currentPage === 1 ? btnDisabled : btnNormal}" ${currentPage === 1 ? 'disabled' : ''} onclick="(${onPageClick.toString()})(${currentPage - 1})">‹</button>`;
  const range = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - currentPage) <= 1) range.push(i);
    else if (range[range.length - 1] !== '…') range.push('…');
  }
  range.forEach(p => {
    if (p === '…') pagesHtml += `<span style="padding:4px 4px;font-size:12px;color:#aaa;align-self:center">…</span>`;
    else pagesHtml += `<button style="${p === currentPage ? btnActive : btnNormal}" ${p === currentPage ? 'disabled' : ''} onclick="(${onPageClick.toString()})(${p})">${p}</button>`;
  });
  pagesHtml += `<button style="${currentPage === totalPages ? btnDisabled : btnNormal}" ${currentPage === totalPages ? 'disabled' : ''} onclick="(${onPageClick.toString()})(${currentPage + 1})">›</button>`;

  el.innerHTML = `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:6px 0;width:100%">
    <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">${pagesHtml}</div>
    ${sizeHtml}
  </div>`;
}
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
  _pipelineRunning = false;
  // Update progress bar to reflect this PDF's stage (always switch, don't prevent downgrade)
  fetch('/admin-panel/api/approve-pdf/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify({ filename: name })
  })
  .then(r => r.json())
  .then(data => {
    if (!data.error) {
      const serverStage = data.stage || 'uploaded';
      // Always switch to this PDF's stage when user clicks a row
      setTrackedPdf(name, serverStage);
    }
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
    // Always update progress bar to this PDF before switching panel
    _trackedPdf = pdfName.replace(/\.pdf$/i, '');
    fetch('/admin-panel/api/approve-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: pdfName })
    })
    .then(r => r.json())
    .then(data => { if (!data.error) setTrackedPdf(pdfName, data.stage || 'uploaded'); })
    .catch(() => {});
  }
  showPanel('families');
}

async function openChunkingFromDashboard(name, btn) {
  // Switch panel manually so we can await loadExistingUploads before scrolling
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
  document.querySelectorAll('.step-item').forEach(s => s.classList.remove('active'));
  document.getElementById('panel-upload').classList.add('active');
  document.querySelector('.sidebar-link[data-panel="upload"]')?.classList.add('active');
  document.querySelector('.step-item[data-step="upload"]')?.classList.add('active');
  closeSidebar();

  await Promise.all([loadExistingUploads(), loadPdfPreview(name)]);
  document.getElementById('pdf-preview-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  runChunkingForPdf(name, btn);
}

async function runChunkingForPdf(name, btnEl) {
  if (_pipelineRunning) { return; }
  selectedPdf = name;
  const row = document.querySelector(`#pdf-selector-table-body tr[data-pdf="${name}"]`);
  if (row) {
    selectPdfRow(row, name);
  }
  _activeChunkingBtn = btnEl;
  await runChunking();
}

async function processWholePdf(btnEl) {
  if (!selectedPdf) { return; }
  if (_pipelineRunning) { return; }
  _pipelineRunning = true;
  btnEl.disabled = true;
  btnEl.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';

  const prog  = document.getElementById('chunk-progress');
  const fill  = document.getElementById('chunk-fill');
  const pct   = document.getElementById('chunk-pct');
  const log   = document.getElementById('chunk-log');
  const label = document.getElementById('chunk-progress-label');

  if (prog) prog.classList.add('visible');
  if (log)  { log.textContent = `▶ Starting extraction for: ${selectedPdf}\n`; log.classList.add('visible'); }

  let p = 0;
  const ticker = setInterval(() => {
    p = Math.min(p + 2, 88);
    if (fill) fill.style.width = p + '%';
    if (pct)  pct.textContent  = p + '%';
  }, 600);

  const isMistral = _visionModel === 'mistral-ocr-latest';
  const endpoint = isMistral ? '/admin-panel/api/pipeline-mistral/' : '/admin-panel/api/pipeline/';
  let success = false;
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({
        filename: selectedPdf,
        parsing_instructions: getParsingInstructions()
      })
    });
    const data = await res.json();
    clearInterval(ticker);
    if (fill) { fill.style.width = '100%'; }
    if (pct)  { pct.textContent = '100%'; }
    if (label){ label.textContent = data.error ? 'Failed' : 'Complete'; }

    // Always show pipeline log output
    const pipelineLog = data.output || data.detail || '';
    if (log && pipelineLog) {
      log.textContent += pipelineLog;
      log.scrollTop = log.scrollHeight;
    }

    if (data.error) {
      const errMsg = getCleanErrorMessage(data.error);
      const lowerErr = errMsg.toLowerCase();
      if (lowerErr.includes('high demand') || lowerErr.includes('quota') || lowerErr.includes('429')) {
        showToast(errMsg, 'error', 10000);
      }
      if (prog) prog.classList.remove('visible');
      if (log) log.classList.remove('visible');
    } else {
      success = true;
      showToast('Product chunks created!', 'success');
      advanceTrackedStage('chunked');
      _setWholePdfDone(btnEl);
      refreshStats();
      loadPdfList();
      const nextEl = document.getElementById('next-to-index');
      if (nextEl) nextEl.style.display = 'flex';
      markStepDone('chunk');
      if (prog) setTimeout(() => { prog.classList.remove('visible'); if (log) log.classList.remove('visible'); }, 3000);
    }
  } catch(e) {
    clearInterval(ticker);
  } finally {
    _pipelineRunning = false;
    btnEl.disabled = false;
    if (!success) btnEl.innerHTML = '<i class="fa fa-layer-group"></i> Process Whole PDF';
    if (fill) fill.style.width = '0%';
    if (pct)  pct.textContent = '0%';
    if (label) label.textContent = 'Extracting products…';
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
  
  // First ensure reviewed products are saved (40% → 60%)
  const STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested'];
  const currentStageIdx = _trackedStage ? STAGE_ORDER.indexOf(_trackedStage) : -1;
  const familiesIdx = STAGE_ORDER.indexOf('families');
  
  if (currentStageIdx < familiesIdx) {
    // Need to review products first
    advanceTrackedStage('families', pdfName);
    await new Promise(resolve => setTimeout(resolve, 500)); // Brief pause to let user see the update
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
      if (audit.indexed_chunks > 0 && audit.failed_chunks === 0) {
        showToast('All chunks are already indexed.', 'info');
        const row = document.querySelector(`#pdf-selector-table-body tr[data-pdf="${pdfName}"]`);
        const btn = row ? row.querySelector('.embed-btn') : null;
        if (btn && !btn.disabled) {
          btn.disabled = true;
          btn.style.cssText = 'background:#22c55e;color:#fff;opacity:1;cursor:not-allowed';
          btn.innerHTML = '<i class="fa fa-check-circle"></i> Embedded';
        }
      } else {
        showToast('No chunks available to embed. Try re-running the pipeline.', 'error');
      }
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
      
      // Update progress to 'indexed' (80%)
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

      // Immediately update this row's button in the DOM
      const row = document.querySelector(`#pdf-selector-table-body tr[data-pdf="${pdfName}"]`);
      const btn = row ? row.querySelector('.embed-btn') : null;
      if (btn) {
        btn.disabled = true;
        btn.style.cssText = 'background:#22c55e;color:#fff;opacity:1;cursor:not-allowed';
        btn.innerHTML = '<i class="fa fa-check-circle"></i> Embedded';
      }

      const displayPdf = _trackedPdf || pdfName.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || pdfName;
      _trackedPdf = displayPdf;
      await refreshStats();
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

// ── Group Embedding (all split parts) ───────────────────────────────────────
async function triggerGroupEmbedding(stem, docIds, btnEl) {
  if (!docIds || !docIds.length) {
    showToast('No document IDs found for this group.', 'error');
    return;
  }
  btnEl.disabled = true;
  btnEl.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Indexing…';

  let totalIndexed = 0;
  let failed = 0;

  for (const docId of docIds) {
    try {
      const auditRes = await fetch(`/admin-panel/api/v2/documents/${docId}/index-audit/`);
      const audit = await auditRes.json();
      if (!auditRes.ok || audit.error) { failed++; continue; }
      if (audit.pending_embeddings === 0) continue;  // already fully indexed
      if (audit.review_blocked > 0) {
        // Force-index anyway — review_blocked is informational in this pipeline
      }

      const res = await fetch(`/admin-panel/api/v2/documents/${docId}/index/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
        body: JSON.stringify({ confirmed_embedding_count: audit.pending_embeddings })
      });
      const data = await res.json();
      if (!res.ok || data.error) { failed++; continue; }
      totalIndexed += data.indexed || 0;
    } catch(e) { failed++; }
  }

  if (failed > 0 && totalIndexed === 0) {
    showToast(`Embedding failed for all parts of "${stem}".`, 'error');
    btnEl.disabled = false;
    btnEl.innerHTML = '<i class="fa fa-brain"></i> Create Embedding';
    return;
  }

  // Update stage
  try {
    await fetch('/admin-panel/api/update-pdf-stage/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename: stem, stage: 'indexed' })
    });
  } catch(e) {}

  const msg = failed > 0
    ? `Indexed ${totalIndexed} chunk(s) for "${stem}" (${failed} part(s) skipped).`
    : `Indexed ${totalIndexed} chunk(s) for "${stem}".`;
  showToast(msg, failed > 0 ? 'info' : 'success');

  btnEl.disabled = true;
  btnEl.style.cssText = 'background:#22c55e;color:#fff;opacity:1;cursor:not-allowed';
  btnEl.innerHTML = '<i class="fa fa-check-circle"></i> Embedded';

  const displayPdf = _trackedPdf || stem;
  _trackedPdf = displayPdf;
  await refreshStats();
  await loadPdfList();
}

// ── Create Chunks (Pipeline) ──────────────────────────────────────────────────
async function runChunking() {
  if (!selectedPdf || _pipelineRunning) { return; }
  _pipelineRunning = true;

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

  const isMistral = _visionModel === 'mistral-ocr-latest';
  const endpoint = isMistral ? '/admin-panel/api/pipeline-mistral/' : '/admin-panel/api/pipeline/';

  try {
    const res  = await fetch(endpoint, {
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

    const pipelineLog = data.output || data.detail || '';
    if (pipelineLog) { log.textContent += pipelineLog; log.scrollTop = log.scrollHeight; }

    if (data.error) {
      const errMsg = getCleanErrorMessage(data.error);
      const lowerErr = errMsg.toLowerCase();
      if (lowerErr.includes('high demand') || lowerErr.includes('quota') || lowerErr.includes('429')) {
        showToast(errMsg, 'error', 10000);
      }
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
    prog.classList.remove('visible');
    log.classList.remove('visible');
  } finally {
    _pipelineRunning = false;
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
let _visionModel = 'gemini-2.5-flash';
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
  const placeholders = { openai: 'an OpenAI', gemini: 'a Gemini', mistral: 'a Mistral' };
  input.placeholder = keyInfo.configured ? 'Leave blank to keep current key' : `Enter ${placeholders[provider] || 'an API'} key`;
}

function onVisionModelChange() {
  const model = document.getElementById('vision-model')?.value || '';
  const mistralCard = document.getElementById('mistral-settings-card');
  if (mistralCard) mistralCard.style.display = model === 'mistral-ocr-latest' ? '' : 'none';
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
    _visionModel = data.configuration.vision_model || 'gemini-2.5-flash';
    _savedChatModel = data.configuration.chat_model;
    fillModelSelect('embedding-model', data.options.embedding_models, data.configuration.embedding_model);
    fillModelSelect('vision-model', data.options.vision_models, data.configuration.vision_model);
    onVisionModelChange();
    document.getElementById('chat-provider').value = data.configuration.chat_provider;
    refreshChatModelOptions(data.configuration.chat_model);
    setKeyStatus('openai', data.keys.openai);
    setKeyStatus('gemini', data.keys.gemini);
    if (data.keys.mistral) setKeyStatus('mistral', data.keys.mistral);
    document.getElementById('clear-openai-key').checked = false;
    document.getElementById('clear-gemini-key').checked = false;
    const clearMistral = document.getElementById('clear-mistral-key');
    if (clearMistral) clearMistral.checked = false;
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
    embedding_model: document.getElementById('embedding-model').value,
    openai_api_key: document.getElementById('openai-api-key').value.trim(),
    gemini_api_key: document.getElementById('gemini-api-key').value.trim(),
    mistral_api_key: (document.getElementById('mistral-api-key')?.value || '').trim(),
    clear_openai_key: document.getElementById('clear-openai-key').checked,
    clear_gemini_key: document.getElementById('clear-gemini-key').checked,
    clear_mistral_key: document.getElementById('clear-mistral-key')?.checked || false,
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
    _visionModel = data.configuration.vision_model || 'gemini-2.5-flash';
    _savedChatModel = data.configuration.chat_model;
    setKeyStatus('openai', data.keys.openai);
    setKeyStatus('gemini', data.keys.gemini);
    if (data.keys.mistral) setKeyStatus('mistral', data.keys.mistral);
    document.getElementById('clear-openai-key').checked = false;
    document.getElementById('clear-gemini-key').checked = false;
    const clearMistralEl = document.getElementById('clear-mistral-key');
    if (clearMistralEl) clearMistralEl.checked = false;
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
  if (!await showConfirm('Delete PDF', `Delete "${filename}" and ALL associated chunks, products and index data? This cannot be undone.`)) return;
  try {
    const res  = await fetch('/admin-panel/api/delete-pdf/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ filename })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast(data.message, 'success');
    await Promise.all([refreshStats(), loadPdfList()]);
  } catch(e) { showToast('Delete failed.', 'error'); }
}

async function deleteEmbeddingsOnly(filename) {
  if (!await showConfirm('Delete Embeddings', `Delete all embeddings for "${filename}"? Chunks and data will be kept.`)) return;
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
    // Force fresh fetch by clearing cache, then reload table + stats
    _pdfDetails = [];
    await loadPdfList();
    await refreshStats();
    if (typeof loadIndexPanel === 'function') loadIndexPanel();
  } catch(e) { showToast('Delete failed.', 'error'); }
}

// -- Stats refresh -------------------------------------------------------------────────────
async function openUploadPreview(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
  document.querySelectorAll('.step-item').forEach(s => s.classList.remove('active'));
  document.getElementById('panel-upload').classList.add('active');
  document.querySelector('.sidebar-link[data-panel="upload"]')?.classList.add('active');
  document.querySelector('.step-item[data-step="upload"]')?.classList.add('active');
  closeSidebar();
  await loadExistingUploads();
  // Check if this is a parent stem (no split-part suffix) — if so, load all its parts
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  if (!splitRe.test(name)) {
    const stem = name.replace(/\.pdf$/i, '');
    try {
      await ensurePdfDetails();
      const parts = (_pdfDetails || []).map(d => d.name).filter(p => splitRe.test(p) && _rootStem(p) === stem);
      if (parts.length) {
        await previewParentGroup(stem, parts);
        document.getElementById('pdf-preview-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
    } catch(e) {}
  }
  await loadPdfPreview(name);
  document.getElementById('pdf-preview-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function selectPdfFromOverview(pdfName) {
  // Immediately remove highlight from all rows and highlight clicked row (no delay)
  document.querySelectorAll('.catalog-table tbody tr').forEach(row => {
    row.style.backgroundColor = '';
  });
  
  // Immediately highlight the clicked row
  event.currentTarget.style.backgroundColor = 'rgba(220, 252, 231, 0.6)'; // light green
  
  // Then fetch the PDF's current stage and update progress bar
  fetch('/admin-panel/api/approve-pdf/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify({ filename: pdfName })
  })
  .then(r => r.json())
  .then(data => {
    if (!data.error) {
      const serverStage = data.stage || 'uploaded';
      const STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested'];
      const currentStageIdx = _trackedStage ? STAGE_ORDER.indexOf(_trackedStage) : -1;
      const serverStageIdx = STAGE_ORDER.indexOf(serverStage);
      
      // Update to server stage (allow both upgrade and showing current stage)
      if (currentStageIdx === -1 || serverStageIdx >= currentStageIdx || _trackedPdf !== pdfName) {
        setTrackedPdf(pdfName, serverStage);
      }
    }
  })
  .catch(() => {});
}

function _overviewGroupRows(items, isProcessed) {
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  const groups = {};   // rootStem -> [item, ...]
  const plain  = [];

  items.forEach(item => {
    if (splitRe.test(item.name)) {
      const root = _rootStem(item.name);
      (groups[root] = groups[root] || []).push(item);
    } else {
      plain.push(item);
    }
  });

  // For plain items whose stem matches a group key, promote them as group parents
  // (shouldn't normally happen, but guard anyway)
  const rows = [];

  plain.forEach(item => {
    const stem = item.name.replace(/\.pdf$/i, '');
    if (groups[stem]) {
      rows.push({ type: 'group-parent', stem, item, children: groups[stem] });
      delete groups[stem];
    } else {
      rows.push({ type: 'plain', item });
    }
  });

  // Any remaining groups (no matching plain parent)
  Object.entries(groups).forEach(([stem, children]) => {
    // Synthesise a virtual parent from aggregated child data
    const totalChunks   = children.reduce((s, c) => s + (c.chunks || 0), 0);
    const totalProducts = children.reduce((s, c) => s + (c.products || 0), 0);
    const allReady = children.every(c => c.chunks > 0);
    const anyProcessing = children.some(c => c.status === 'Processing');
    const anyFailed = children.some(c => c.status === 'Failed');
    const anyReady = children.some(c => c.chunks > 0);
    const synth = {
      name: stem,
      chunks: totalChunks,
      products: totalProducts,
      status: allReady ? 'Ready' : anyProcessing ? 'Processing' : anyReady ? 'Partial' : anyFailed ? 'Failed' : 'Pending',
    };
    rows.push({ type: 'group-parent', stem, item: synth, children });
  });

  return rows;
}

function _toggleOverviewGroup(stem) {
  const btn = document.getElementById('ovg-btn-' + stem);
  const children = document.querySelectorAll('.ovg-child-' + CSS.escape(stem));
  const isOpen = btn && btn.classList.contains('open');
  if (btn) {
    btn.classList.toggle('open', !isOpen);
    btn.querySelector('i').className = 'fa fa-chevron-' + (isOpen ? 'right' : 'down');
  }
  children.forEach(r => r.classList.toggle('visible', !isOpen));
}

function updateCatalogTable(processed, unprocessed) {
  const container = document.getElementById('catalog-overview-body');
  if (!container) return;

  unprocessed = unprocessed || [];

  // Merge split parts from unprocessed into their processed group if same root stem exists
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  // If a processed parent exists for a split family, keep the split parts with it.
  // We use all processed roots here, not just processed split files, so a plain
  // parent PDF like "Cordless_Drill.pdf" can absorb its child split parts.
  const processedStems = new Set(
    processed.map(p => _rootStem(p.name))
  );
  const mergedUnprocessed = [];
  unprocessed.forEach(item => {
    if (splitRe.test(item.name)) {
      const rootStem = _rootStem(item.name);
      if (processedStems.has(rootStem)) {
        // inject into processed list so it appears as child of that group
        processed.push(item);
        return;
      }
    }
    mergedUnprocessed.push(item);
  });
  unprocessed = mergedUnprocessed;

  if (!processed.length && !unprocessed.length) {
    let emptyHtml = `
      <div style="text-align:center;padding:48px 20px;color:var(--grey)">
        <i class="fa fa-inbox" style="font-size:40px;margin-bottom:14px;display:block;color:#d0d0d0"></i>
        <p style="font-size:14px;font-weight:600;margin-bottom:8px">No PDFs processed yet</p>
    `;
    if (IS_ADMIN) {
      emptyHtml += `<p style="font-size:13px;margin-bottom:20px">Follow the 5-step workflow to get started.</p>
        <button class="btn btn-primary" onclick="showPanel('upload')"><i class="fa fa-upload"></i> Upload Your First PDF</button>`;
    } else {
      emptyHtml += `<p style="font-size:13px">No catalog data available yet. Please contact your administrator.</p>`;
    }
    emptyHtml += `</div>`;
    container.innerHTML = emptyHtml;
    return;
  }

  let tableHtml = `
    <div class="table-wrap">
      <table class="catalog-table">
        <thead><tr>
          <th>PDF Name</th><th>Chunks</th><th>Products</th><th>Status</th><th>Actions</th>
        </tr></thead>
        <tbody>
  `;

  // ── Processed rows (grouped) ──
  _overviewGroupRows(processed, true).forEach(row => {
    if (row.type === 'plain') {
      const item = row.item;
      tableHtml += `
        <tr style="cursor:pointer" onclick="selectPdfFromOverview('${escHtml(item.name)}')">
          <td><div class="pdf-name-cell"><i class="fa fa-file-pdf"></i><span class="pname" style="cursor:pointer" onclick="event.stopPropagation();openUploadPreview('${escHtml(item.name)}')">${escHtml(item.name)}</span></div></td>
          <td><span class="badge badge-blue">${item.chunks}</span></td>
          <td><span class="badge ${item.products > 0 ? 'badge-green' : 'badge-grey'}">${item.products || 0}</span></td>
          <td><span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span></td>
          <td onclick="event.stopPropagation()">
            <div class="table-actions">
              <button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(item.name)}')">
                <i class="fa fa-layer-group"></i> Chunks</button>
              ${IS_ADMIN && item.chunks > 0 ? `<button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(item.name)})'><i class="fa fa-sitemap"></i> Families</button>` : ''}
              ${IS_ADMIN ? `<button class="btn btn-sm btn-danger" onclick="deletePdf('${escHtml(item.name)}')" onclick="event.stopPropagation()"><i class="fa fa-trash"></i></button>` : ''}
            </div>
          </td>
        </tr>`;
    } else {
      // Group parent row
      const { stem, item, children } = row;
      const safeStem = escHtml(stem);
      const statusBadge = item.status === 'Ready'
        ? `<span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span>`
        : item.status === 'Partial'
        ? `<span class="badge badge-yellow"><i class="fa fa-adjust"></i> Partial</span>`
        : item.status === 'Processing'
        ? `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`
        : `<span class="badge badge-grey"><i class="fa fa-clock"></i> Pending</span>`;
      tableHtml += `
        <tr class="pdf-group-parent" style="cursor:pointer" onclick="selectPdfFromOverview('${safeStem}')">
          <td>
            <div class="pdf-name-cell" style="gap:6px">
              <i class="fa fa-file-pdf"></i>
              <span class="pname" style="cursor:pointer;color:var(--orange);text-decoration:underline" onclick="event.stopPropagation();openUploadPreview('${safeStem}.pdf')">${safeStem}</span>
              <span class="split-count-badge">${children.length} parts</span>
            </div>
          </td>
          <td><span class="badge badge-blue">${item.chunks}</span></td>
          <td><span class="badge ${item.products > 0 ? 'badge-green' : 'badge-grey'}">${item.products || 0}</span></td>
          <td>${statusBadge}</td>
          <td onclick="event.stopPropagation()">
            <div class="table-actions">
              <button class="btn btn-sm btn-secondary" onclick="openChunks('${safeStem}')">
                <i class="fa fa-layer-group"></i> Chunks</button>
              ${IS_ADMIN && item.chunks > 0 ? `<button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(stem)})'><i class="fa fa-sitemap"></i> Families</button>` : ''}
              ${IS_ADMIN ? `<button class="btn btn-sm btn-danger" onclick="deletePdf('${safeStem}.pdf')"><i class="fa fa-trash"></i></button>` : ''}
              <button class="pdf-group-expand-btn" id="ovg-btn-${safeStem}" onclick="event.stopPropagation();_toggleOverviewGroup('${safeStem}')" title="Show split parts"><i class="fa fa-chevron-right"></i></button>
            </div>
          </td>
        </tr>`;
      // Child rows (hidden by default)
      children.forEach(child => {
        const childReady = child.chunks > 0;
        const childStatusBadge = childReady
          ? `<span class="badge badge-green"><i class="fa fa-check-circle"></i> Ready</span>`
          : child.status === 'Processing'
          ? `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`
          : child.status === 'Failed'
          ? `<span class="badge badge-red"><i class="fa fa-times-circle"></i> Failed</span>`
          : `<span class="badge badge-grey"><i class="fa fa-clock"></i> Pending</span>`;
        tableHtml += `
          <tr class="pdf-child-row ovg-child-${escHtml(stem)}">
            <td><div class="pdf-name-cell"><i class="fa fa-file-pdf" style="color:#f97316;font-size:13px"></i><span style="font-size:12px;color:#555;cursor:pointer" onclick="event.stopPropagation();openUploadPreview('${escHtml(child.name)}')">${escHtml(child.name)}</span></div></td>
            <td><span class="badge badge-blue">${child.chunks || 0}</span></td>
            <td><span class="badge ${(child.products || 0) > 0 ? 'badge-green' : 'badge-grey'}">${child.products || 0}</span></td>
            <td>${childStatusBadge}</td>
            <td>
              <div class="table-actions">
                ${childReady ? `
                  <button class="btn btn-sm btn-secondary" onclick="openChunks('${escHtml(child.name)}')" style="font-size:11px;padding:5px 10px"><i class="fa fa-layer-group"></i> Chunks</button>
                  ${IS_ADMIN && child.chunks > 0 ? `<button class="btn btn-sm btn-secondary" onclick='openFamilies(${JSON.stringify(child.name)})' style="font-size:11px;padding:5px 10px"><i class="fa fa-sitemap"></i> Families</button>` : ''}
                ` : ''}
                ${IS_ADMIN ? `<button class="btn btn-sm btn-danger" onclick="deletePdf('${escHtml(child.name)}')" style="font-size:11px;padding:5px 10px"><i class="fa fa-trash"></i></button>` : ''}
              </div>
            </td>
          </tr>`;
      });
    }
  });

  // ── Unprocessed rows (grouped) ──
  _overviewGroupRows(unprocessed, false).forEach(row => {
    if (row.type === 'plain') {
      const item = row.item;
      const statusBadge = item.status === 'Processing'
        ? `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`
        : item.status === 'Failed'
        ? `<span class="badge badge-red"><i class="fa fa-times-circle"></i> Failed</span>`
        : `<span class="badge" style="background:#f1f5f9;color:#64748b"><i class="fa fa-clock"></i> Pending</span>`;
      tableHtml += `
        <tr style="opacity:0.75">
          <td><div class="pdf-name-cell"><i class="fa fa-file-pdf" style="color:#aaa"></i><span class="pname">${escHtml(item.name)}</span></div></td>
          <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
          <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
          <td>${statusBadge}</td>
          <td>${IS_ADMIN ? `<div class="table-actions">
            <button class="btn btn-sm btn-primary" onclick="openChunkingFromDashboard('${escHtml(item.name)}',this)"><i class="fa fa-layer-group"></i> Create Chunks</button>
            <button class="btn btn-sm btn-danger" onclick="deletePdf('${escHtml(item.name)}')" ><i class="fa fa-trash"></i></button>
          </div>` : ''}</td>
        </tr>`;
    } else {
      const { stem, item, children } = row;
      const safeStem = escHtml(stem);
      const statusBadge = item.status === 'Processing'
        ? `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`
        : item.status === 'Failed'
        ? `<span class="badge badge-red"><i class="fa fa-times-circle"></i> Failed</span>`
        : `<span class="badge" style="background:#f1f5f9;color:#64748b"><i class="fa fa-clock"></i> Pending</span>`;
      tableHtml += `
        <tr class="pdf-group-parent" style="opacity:0.85">
          <td>
            <div class="pdf-name-cell" style="gap:6px">
              <i class="fa fa-file-pdf" style="color:#aaa"></i>
              <span class="pname">${safeStem}</span>
              <span class="split-count-badge">${children.length} parts</span>
            </div>
          </td>
          <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
          <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
          <td>${statusBadge}</td>
          <td>${IS_ADMIN ? `<div class="table-actions">
            <button class="btn btn-sm btn-primary" onclick="showPanel('chunk')"><i class="fa fa-layer-group"></i> Create Chunks</button>
            <button class="btn btn-sm btn-danger" onclick="deletePdf('${safeStem}.pdf')"><i class="fa fa-trash"></i></button>
            <button class="pdf-group-expand-btn" id="ovg-btn-${safeStem}" onclick="event.stopPropagation();_toggleOverviewGroup('${safeStem}')" title="Show split parts"><i class="fa fa-chevron-right"></i></button>
          </div>` : ''}</td>
        </tr>`;
      children.forEach(child => {
        const cStatus = child.status === 'Processing'
          ? `<span class="badge badge-yellow"><i class="fa fa-spinner fa-spin"></i> Processing</span>`
          : child.status === 'Failed'
          ? `<span class="badge badge-red"><i class="fa fa-times-circle"></i> Failed</span>`
          : `<span class="badge" style="background:#f1f5f9;color:#64748b"><i class="fa fa-clock"></i> Pending</span>`;
        tableHtml += `
          <tr class="pdf-child-row ovg-child-${escHtml(stem)}">
            <td><div class="pdf-name-cell"><i class="fa fa-file-pdf" style="color:#f97316;font-size:13px"></i><span style="font-size:12px;color:#555;cursor:pointer" onclick="event.stopPropagation();openUploadPreview('${escHtml(child.name)}')">${escHtml(child.name)}</span></div></td>
            <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
            <td><span class="badge" style="background:#f1f5f9;color:#94a3b8">&mdash;</span></td>
            <td>${cStatus}</td>
            <td>${IS_ADMIN ? `<div class="table-actions">
              <button class="btn btn-sm btn-primary" onclick="openChunkingFromDashboard('${escHtml(child.name)}',this)" style="font-size:11px;padding:5px 10px"><i class="fa fa-layer-group"></i> Create Chunks</button>
              <button class="btn btn-sm btn-danger" onclick="deletePdf('${escHtml(child.name)}')" style="font-size:11px;padding:5px 10px"><i class="fa fa-trash"></i></button>
            </div>` : ''}</td>
          </tr>`;
      });
    }
  });

  tableHtml += `</tbody></table></div>`;
  let tableWrap = document.getElementById('overview-table-wrap');
  if (!tableWrap) {
    // First JS render: wipe static server-rendered table, insert our managed wrap
    container.innerHTML = '<div id="overview-table-wrap"></div>';
    tableWrap = document.getElementById('overview-table-wrap');
  }
  tableWrap.innerHTML = tableHtml;
  _initOverviewSearch();
}

// ── Dashboard overview search + pagination ────────────────────────────────────
const _overviewState = { page: 1, pageSize: 15, query: '', status: '' };

function _overviewSetPageSize(n) {
  _overviewState.pageSize = n;
  _overviewState.page = 1;
  _renderOverviewPage();
}

function _initOverviewSearch() {
  const container = document.getElementById('catalog-overview-body');
  if (!container) return;
  if (!document.getElementById('overview-search-bar')) {
    const bar = document.createElement('div');
    bar.className = 'panel-search-bar';
    bar.innerHTML = `
      <div style="position:relative;flex:1;min-width:180px;max-width:280px">
        <i class="fa fa-search" style="position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#aaa;font-size:13px"></i>
        <input type="text" id="overview-search-bar" placeholder="Search PDFs…" oninput="_overviewFilter()" class="panel-search-input">
      </div>
      <div style="position:relative;min-width:140px">
        <select id="overview-status-filter" onchange="_overviewFilter()" class="panel-search-input" style="padding-left:10px;cursor:pointer;appearance:none;padding-right:28px">
          <option value="">All statuses</option>
          <option value="ready">Ready</option>
          <option value="pending">Pending</option>
          <option value="processing">Processing</option>
          <option value="failed">Failed</option>
          <option value="partial">Partial</option>
        </select>
        <i class="fa fa-chevron-down" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);color:#aaa;font-size:11px;pointer-events:none"></i>
      </div>
      <span id="overview-count" class="panel-search-count"></span>`;
    container.insertBefore(bar, container.firstChild);
  }
  if (!document.getElementById('overview-pagination')) {
    const pg = document.createElement('div');
    pg.id = 'overview-pagination';
    pg.className = 'panel-pagination';
    container.appendChild(pg);
  }
  _overviewFilter();
}

function _overviewFilter() {
  const q = (document.getElementById('overview-search-bar')?.value || '').toLowerCase().trim();
  const s = (document.getElementById('overview-status-filter')?.value || '').toLowerCase().trim();
  const changed = q !== _overviewState.query || s !== _overviewState.status;
  _overviewState.query = q;
  _overviewState.status = s;
  if (changed) _overviewState.page = 1;
  _renderOverviewPage();
}

function _renderOverviewPage() {
  const tbody = document.querySelector('#catalog-overview-body .catalog-table tbody');
  if (!tbody) return;
  const q = _overviewState.query;
  const s = _overviewState.status;
  const allRows = Array.from(tbody.querySelectorAll('tr:not(.ovg-child-hidden-placeholder)'));
  const topRows = allRows.filter(r => !r.className.startsWith('pdf-child-row'));
  const filtered = topRows.filter(r => {
    const text = (r.textContent || '').toLowerCase();
    const matchQ = !q || text.includes(q);
    const matchS = !s || text.includes(s);
    return matchQ && matchS;
  });
  const pageSize = _overviewState.pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  _overviewState.page = Math.min(_overviewState.page, totalPages);
  const start = (_overviewState.page - 1) * pageSize;
  const pageRows = new Set(filtered.slice(start, start + pageSize));
  allRows.forEach(r => {
    if (r.className.startsWith('pdf-child-row')) return;
    r.style.display = pageRows.has(r) ? '' : 'none';
  });
  const countEl = document.getElementById('overview-count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} PDF${filtered.length !== 1 ? 's' : ''}` : 'No results';
  _renderPagination('overview-pagination', _overviewState.page, totalPages,
    p => { _overviewState.page = p; _renderOverviewPage(); },
    _overviewState.pageSize, '_overviewSetPageSize');
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
    updateCatalogTable(data.processed || [], data.unprocessed || []);
  } catch(e) {
    console.error('Error refreshing stats:', e);
  }
}

function _num(value) {
  const parsed = parseInt(String(value ?? '0').replace(/[^0-9]/g, ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

// ── Per-PDF progress tracking ─────────────────────────────────────────────────
// stages: uploaded=20%  chunked=40%  families=60%  indexed=80%  tested=100%
let _trackedPdf     = null;
let _trackedStage   = null;
let _trackedPercent = null;  // custom percent for partial-split progress
let _trackedSetAt   = 0;     // timestamp when _trackedPdf was last set locally
let _approvedAt     = 0;     // timestamp when approve-pdf was last called

const STAGE_CONFIG = {
  uploaded: { percent: 20,  stepNo: 1, active: 'upload',   title: 'PDF uploaded & split',  sub: 'Preview the PDF then split & parse to extract product chunks.' },
  chunked:  { percent: 40,  stepNo: 2, active: 'chunk',    title: 'Chunks created',         sub: 'Product chunks are ready. Review products before indexing.' },
  families: { percent: 60,  stepNo: 3, active: 'families', title: 'Products reviewed',      sub: 'Review products, variants, and the final chunk mapping.' },
  indexed:  { percent: 80,  stepNo: 4, active: 'index',    title: 'Chunks embedded',        sub: 'Approved products are indexed for semantic search.' },
  tested:   { percent: 100, stepNo: 5, active: 'chat',     title: 'Admin approved!',        sub: 'All steps complete. The catalog is live for product queries.' },
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
  // On fresh page load (when _trackedPdf is null), restore from server if available
  if (!_trackedPdf) {
    if (data.tracked_pdf && data.tracked_stage) {
      _trackedPdf   = data.tracked_pdf;
      _trackedStage = data.tracked_stage;
      _trackedSetAt = Date.now();
      const pct = data.tracked_percent !== undefined ? data.tracked_percent : undefined;
      if (pct !== undefined) {
        _renderProgress(data.tracked_pdf, data.tracked_stage, pct);
      } else {
        _renderProgress(data.tracked_pdf, data.tracked_stage);
      }
      if (data.families_progress && data.tracked_stage === 'chunked') {
        const { total, approved } = data.families_progress;
        document.getElementById('workflow-progress-sub').textContent =
          `${approved} of ${total} products approved. Approve all products to advance to 60%.`;
      }
    }
    return;
  }
  
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
        if (pct <= 60) {
          const chunkedParts = Math.round((pct - 40) / 20 * totalParts);
          sub = `${chunkedParts} of ${totalParts} parts chunked. Continue processing remaining parts.`;
        } else {
          const indexedParts = Math.round((pct - 60) / 20 * totalParts);
          sub = `${indexedParts} of ${totalParts} parts indexed. Continue embedding remaining parts.`;
        }
        document.getElementById('workflow-progress-sub').textContent = sub;
      } else if (data.families_progress && data.tracked_stage === 'chunked') {
        // Show product approval progress
        const { total, approved } = data.families_progress;
        const sub = `${approved} of ${total} products approved. Approve all products to advance to 60%.`;
        document.getElementById('workflow-progress-sub').textContent = sub;
      }
    } else {
      _renderProgress(data.tracked_pdf, data.tracked_stage);
    }
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

async function showSplitterForPdf(filename) {
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
      await ensurePdfDetails().catch(() => {});
    }
    _updateWholePdfStatus(filename);
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

document.addEventListener('DOMContentLoaded', async () => {
  fetch('/admin-panel/api/model-config/').then(r => r.ok ? r.json() : null).then(d => { if (d && d.configuration) _visionModel = d.configuration.vision_model || _visionModel; }).catch(() => {});
  // On fresh page load, completely reset all progress tracking state
  _trackedPdf = null;
  _trackedStage = null;
  _trackedPercent = null;
  _trackedSetAt = 0;
  _userSelectedPdf = null;
  _resetProgress();
  await Promise.all([refreshStats(), loadPdfList()]);  // preload PDF cache so Upload panel opens instantly
  await loadExistingUploads();
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

function renderSplitParts(parts, skipAutoPreview = false) {
  const wrap  = document.getElementById('split-parts-wrap');
  const list  = document.getElementById('split-parts-list');
  const title = document.getElementById('split-parts-title');

  wrap.style.display = 'block';
  title.textContent  = `${parts.length} parts ready to process`;
  document.getElementById('next-to-chunk').style.display = 'none';
  const wholeBtn = document.getElementById('btn-process-whole-pdf');
  if (wholeBtn) { wholeBtn.style.display = ''; wholeBtn.disabled = false; }
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
  if (parts.length && !skipAutoPreview) loadSplitPartPreview(0);
}

async function runSingleSplit(idx, options = {}) {
  const fromBatch = !!options.fromBatch;
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
  if (processAllBtn && !fromBatch) { processAllBtn.disabled = true; processAllBtn.style.opacity = '0.5'; }

  try {
    const isMistral = _visionModel === 'mistral-ocr-latest';
    const splitEndpoint = isMistral ? '/admin-panel/api/pipeline-mistral-split/' : '/admin-panel/api/pipeline-split/';
    const res  = await fetch(splitEndpoint, {
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
      const errMsg = getCleanErrorMessage(data.error);
      const lowerErr = errMsg.toLowerCase();
      if (lowerErr.includes('high demand') || lowerErr.includes('quota') || lowerErr.includes('429')) {
        showToast(errMsg, 'error', 10000);
      }
      return false;
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
        const percent = 20 + (doneParts / totalParts) * 20;
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
      return true;
    }
  } catch(e) {
    itemEl.className = 'split-part-item errored';
    statEl.className = 'split-part-status errored';
    statEl.textContent = '✗ Error';
    return false;
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
    if (processAllBtn2 && !fromBatch) { processAllBtn2.disabled = false; processAllBtn2.style.opacity = ''; }
  }
}

async function runAllSplits() {
  const btn = document.getElementById('btn-run-all-splits');
  btn.disabled = true;
  btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Processing…';
  let failedCount = 0;
  for (let i = 0; i < _splitParts.length; i++) {
    const statEl = document.getElementById(`split-status-${i}`);
    if (statEl && statEl.textContent === '✓ Done') continue; // skip already done
    const ok = await runSingleSplit(i, { fromBatch: true });
    if (!ok) failedCount += 1;
  }

  const statuses = _splitParts.map((_, i) => document.getElementById(`split-status-${i}`)?.textContent || '');
  const doneCount = statuses.filter(text => text === '✓ Done').length;
  const allDone = _splitParts.length > 0 && doneCount === _splitParts.length;
  const nextToIndex = document.getElementById('next-to-index');

  btn.disabled = false;
  btn.style.opacity = '';

  if (allDone) {
    btn.innerHTML = '<i class="fa fa-check"></i> All Done';
    if (nextToIndex) nextToIndex.style.display = 'flex';
    showToast('Product chunks created!', 'success');
    return;
  }

  btn.innerHTML = '<i class="fa fa-redo"></i> Retry Failed';
  if (nextToIndex) nextToIndex.style.display = 'none';
}

// ── Chunks Drawer ─────────────────────────────────────────────────────
let _chunksCache = {};

async function openChunks(pdfName) {
  // For a nested split part (two levels of suffixes), pass the exact filename so the backend
  // looks up only that specific document — NOT the root stem (which would aggregate everything).
  // For a first-level split part, also pass exact filename — backend handles it via _resolve_catalog_document.
  const lookupName = pdfName;

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

  if (_chunksCache[lookupName]) {
    renderChunks(pdfName, _chunksCache[lookupName]);
    return;
  }

  try {
    const res  = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(lookupName)}`);
    const data = await res.json();
    if (data.error) {
      // If exact lookup failed (e.g. no direct doc), fall back to parent stem
      const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
      if (splitRe.test(lookupName)) {
        const parentStem = lookupName.replace(splitRe, '') + '.pdf';
        const res2 = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(parentStem)}`);
        const data2 = await res2.json();
        if (!data2.error) {
          _chunksCache[lookupName] = data2.chunks;
          renderChunks(pdfName, data2.chunks);
          return;
        }
      }
      body.innerHTML = `<p style="color:var(--orange)">${data.error}</p>`; return;
    }
    _chunksCache[lookupName] = data.chunks;
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
  // Use the exact pdfName for View PDF — backend resolves it correctly
  const viewPdfName = pdfName;

  body.innerHTML = chunks.map((c, i) => `
    <div class="chunk-content${i===0?' visible':''}" id="chunk-pane-${i}">
      <div class="chunk-toolbar">
        <span>${escHtml(c.filename)}</span>
        <div>
          <button class="btn btn-sm btn-secondary" onclick="viewChunkPdf('${escHtml(viewPdfName)}')" title="View PDF pages for verification">
            <i class="fa fa-file-pdf"></i> View PDF
          </button>
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
      body: JSON.stringify({
        pdf: pdfName,
        filename: chunk.filename,
        chunk_id: chunk.id,
        content,
      })
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
  
  // Also close the floating PDF preview when chunks drawer closes
  const floatingPreview = document.getElementById('floating-pdf-preview');
  if (floatingPreview) {
    floatingPreview.style.display = 'none';
  }
}

// Function to open PDF preview while keeping chunks drawer open
async function viewChunkPdf(pdfName) {
  // Create or show a floating PDF preview on the left side
  let floatingPreview = document.getElementById('floating-pdf-preview');
  
  if (!floatingPreview) {
    // Create the floating preview container
    floatingPreview = document.createElement('div');
    floatingPreview.id = 'floating-pdf-preview';
    floatingPreview.style.cssText = `
      position: fixed;
      left: 0;
      top: 0;
      right: 660px;
      height: 100vh;
      background: white;
      z-index: 600;
      box-shadow: 2px 0 16px rgba(0,0,0,0.2);
      overflow: hidden;
      display: flex;
      flex-direction: column;
      border-right: 2px solid var(--orange);
    `;
    
    floatingPreview.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#fafafa;border-bottom:1px solid #e5e5e5;">
        <div style="display:flex;align-items:center;gap:10px;">
          <i class="fa fa-file-pdf" style="color:var(--orange);"></i>
          <span style="font-size:13px;font-weight:600;color:var(--dark);" id="floating-pdf-name"></span>
          <span style="background:var(--orange);color:#fff;font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;" id="floating-page-count"></span>
        </div>
        <button onclick="closeFloatingPdf()" style="width:32px;height:32px;border:1.5px solid #e5e5e5;background:white;border-radius:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;color:#888;">
          <i class="fa fa-times"></i>
        </button>
      </div>
      <div style="display:flex;flex:1;min-height:0;">
        <div id="floating-thumb-strip" style="width:120px;flex-shrink:0;border-right:1px solid #e5e5e5;overflow-y:auto;background:#f4f4f4;"></div>
        <div id="floating-page-viewer" style="flex:1;display:flex;flex-direction:column;overflow:hidden;background:#f0f0f0;"></div>
      </div>
    `;
    
    document.body.appendChild(floatingPreview);
  }
  
  // Show the floating preview
  floatingPreview.style.display = 'flex';
  
  // Load PDF pages
  const nameEl = document.getElementById('floating-pdf-name');
  const countEl = document.getElementById('floating-page-count');
  const strip = document.getElementById('floating-thumb-strip');
  const viewer = document.getElementById('floating-page-viewer');
  
  nameEl.textContent = pdfName;
  countEl.textContent = 'Loading...';
  strip.innerHTML = '<div style="padding:40px 10px;text-align:center;color:#888;font-size:13px;"><i class="fa fa-spinner fa-spin" style="font-size:24px;display:block;margin-bottom:10px;"></i>Loading pages...</div>';
  viewer.innerHTML = '<div style="padding:40px;text-align:center;color:#888;font-size:13px;"><i class="fa fa-file-pdf" style="font-size:32px;display:block;margin-bottom:10px;color:#ccc;"></i>Select a page</div>';
  
  try {
    const res = await fetch('/admin-panel/api/pdf-preview/?pdf=' + encodeURIComponent(pdfName));
    const data = await res.json();
    
    if (data.error) {
      // Parent PDF not on disk — load ALL split parts and concatenate pages
      const splitRe2 = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
      if (!splitRe2.test(pdfName)) {
        try {
          await ensurePdfDetails();
          const allPdfs = (_pdfDetails || []).map(d => d.name);
          const stem = pdfName.replace(/\.pdf$/i, '');
          const parts = allPdfs.filter(p => splitRe2.test(p) && _rootStem(p) === stem).sort();
          if (parts.length) {
            strip.innerHTML = '<div style="padding:40px 10px;text-align:center;color:#888;font-size:13px;"><i class="fa fa-spinner fa-spin" style="font-size:24px;display:block;margin-bottom:10px;"></i>Loading all parts...</div>';
            const allPages = [];
            for (const part of parts) {
              try {
                const pRes = await fetch('/admin-panel/api/pdf-preview/?pdf=' + encodeURIComponent(part));
                const pData = await pRes.json();
                if (!pData.error) {
                  const offset = allPages.length;
                  pData.pages.forEach((pg, i) => allPages.push({ ...pg, num: offset + i + 1 }));
                }
              } catch(e3) {}
            }
            if (allPages.length) {
              countEl.textContent = allPages.length + ' pages';
              // Re-render thumbnails and viewer with all pages
              strip.innerHTML = allPages.map((p, i) => `
                <div class="floating-thumb" onclick="selectFloatingPage(${i})" id="floating-thumb-${i}" style="padding:10px 8px;cursor:pointer;border-bottom:1px solid #e8e8e8;display:flex;flex-direction:column;align-items:center;gap:5px;transition:background .15s;${i===0?'background:var(--orange-light);border-left:3px solid var(--orange);':''}">
                  <img src="${p.thumb}" style="width:88px;height:auto;border:1px solid #ddd;border-radius:3px;box-shadow:0 1px 4px rgba(0,0,0,0.08);display:block;"/>
                  <span style="font-size:11px;color:var(--grey);font-weight:600;">${p.num}</span>
                </div>`).join('');
              viewer.innerHTML = `
                <div style="width:100%;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#fff;border-bottom:1px solid #e5e5e5;flex-shrink:0;">
                  <button onclick="adjustFloatingZoom(-25)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;"><i class="fa fa-minus"></i></button>
                  <span id="floating-zoom-level" style="font-size:12px;font-weight:600;color:var(--grey);min-width:40px;text-align:center;">100%</span>
                  <button onclick="adjustFloatingZoom(25)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;"><i class="fa fa-plus"></i></button>
                  <button onclick="adjustFloatingZoom(0)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;"><i class="fa fa-compress-arrows-alt"></i></button>
                  <span style="font-size:11px;color:var(--grey);margin-left:6px;"><i class="fa fa-mouse"></i> Scroll</span>
                  <span id="floating-current-page" style="font-size:12px;color:var(--grey);font-weight:600;margin-left:auto;">Page 1 of ${allPages.length}</span>
                </div>
                <div id="floating-viewer-scroll" style="flex:1;overflow:auto;padding:16px;box-sizing:border-box;scroll-behavior:smooth;">
                  <div id="floating-page-stack" style="width:100%;margin:0 auto;display:flex;flex-direction:column;gap:18px;transition:width .2s;">
                    ${allPages.map((p, i) => `<figure id="floating-page-${i}" style="width:100%;margin:0;display:flex;flex-direction:column;align-items:center;"><img src="${p.full}" style="width:100%;height:auto;display:block;border:1px solid #e5e5e5;border-radius:4px;box-shadow:0 2px 12px rgba(0,0,0,0.12);background:#fff;"/><figcaption style="margin-top:7px;color:#777;font-size:11px;font-weight:600;">Page ${p.num}</figcaption></figure>`).join('')}
                  </div>
                </div>`;
              window._floatingPages = allPages;
              window._floatingZoom = 100;
              document.getElementById('floating-viewer-scroll')?.addEventListener('scroll', syncFloatingPage, { passive: true });
              return;
            }
          }
        } catch(e2) {}
      }
      strip.innerHTML = `<div style="padding:20px 10px;text-align:center;color:#dc2626;font-size:12px;"><i class="fa fa-exclamation-triangle" style="display:block;margin-bottom:8px;"></i>${data.error}</div>`;
      return;
    }
    
    countEl.textContent = data.total + ' pages';
    
    // Render thumbnails
    strip.innerHTML = data.pages.map((p, i) => `
      <div class="floating-thumb" onclick="selectFloatingPage(${i})" id="floating-thumb-${i}" style="padding:10px 8px;cursor:pointer;border-bottom:1px solid #e8e8e8;display:flex;flex-direction:column;align-items:center;gap:5px;transition:background .15s;${i === 0 ? 'background:var(--orange-light);border-left:3px solid var(--orange);' : ''}">
        <img src="${p.thumb}" style="width:88px;height:auto;border:1px solid #ddd;border-radius:3px;box-shadow:0 1px 4px rgba(0,0,0,0.08);display:block;" />
        <span style="font-size:11px;color:var(--grey);font-weight:600;">${p.num}</span>
      </div>
    `).join('');
    
    // Render continuous scroll view
    viewer.innerHTML = `
      <div style="width:100%;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#fff;border-bottom:1px solid #e5e5e5;flex-shrink:0;">
        <button onclick="adjustFloatingZoom(-25)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;" title="Zoom out">
          <i class="fa fa-minus"></i>
        </button>
        <span id="floating-zoom-level" style="font-size:12px;font-weight:600;color:var(--grey);min-width:40px;text-align:center;">100%</span>
        <button onclick="adjustFloatingZoom(25)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;" title="Zoom in">
          <i class="fa fa-plus"></i>
        </button>
        <button onclick="adjustFloatingZoom(0)" style="width:30px;height:30px;border:1.5px solid #e5e5e5;border-radius:6px;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;" title="Reset">
          <i class="fa fa-compress-arrows-alt"></i>
        </button>
        <span style="font-size:11px;color:var(--grey);margin-left:6px;display:flex;align-items:center;gap:5px;"><i class="fa fa-mouse"></i>Scroll</span>
        <span id="floating-current-page" style="font-size:12px;color:var(--grey);font-weight:600;margin-left:auto;">Page 1 of ${data.pages.length}</span>
      </div>
      <div id="floating-viewer-scroll" style="flex:1;overflow:auto;padding:16px;box-sizing:border-box;scroll-behavior:smooth;">
        <div id="floating-page-stack" style="width:100%;margin:0 auto;display:flex;flex-direction:column;gap:18px;transition:width .2s;">
          ${data.pages.map((p, i) => `
            <figure id="floating-page-${i}" style="width:100%;margin:0;display:flex;flex-direction:column;align-items:center;">
              <img src="${p.full}" style="width:100%;height:auto;display:block;border:1px solid #e5e5e5;border-radius:4px;box-shadow:0 2px 12px rgba(0,0,0,0.12);background:#fff;" />
              <figcaption style="margin-top:7px;color:#777;font-size:11px;font-weight:600;">Page ${p.num}</figcaption>
            </figure>
          `).join('')}
        </div>
      </div>
    `;
    
    window._floatingPages = data.pages;
    window._floatingZoom = 100;
    
    // Add scroll listener
    const scrollEl = document.getElementById('floating-viewer-scroll');
    scrollEl.addEventListener('scroll', syncFloatingPage, { passive: true });
    
  } catch(e) {
    strip.innerHTML = '<div style="padding:20px 10px;text-align:center;color:#dc2626;font-size:12px;"><i class="fa fa-exclamation-triangle" style="display:block;margin-bottom:8px;"></i>Failed to load</div>';
  }
}

function closeFloatingPdf() {
  const floatingPreview = document.getElementById('floating-pdf-preview');
  if (floatingPreview) {
    floatingPreview.style.display = 'none';
  }
}

function selectFloatingPage(idx) {
  const pages = window._floatingPages || [];
  if (!pages[idx]) return;
  
  const scroll = document.getElementById('floating-viewer-scroll');
  const page = document.getElementById('floating-page-' + idx);
  const first = document.getElementById('floating-page-0');
  
  if (scroll && page && first) {
    scroll.scrollTo({ top: page.offsetTop - first.offsetTop, behavior: 'smooth' });
  }
  
  // Update active thumbnail
  document.querySelectorAll('.floating-thumb').forEach((el, i) => {
    if (i === idx) {
      el.style.background = 'var(--orange-light)';
      el.style.borderLeft = '3px solid var(--orange)';
    } else {
      el.style.background = '';
      el.style.borderLeft = '';
    }
  });
  
  const label = document.getElementById('floating-current-page');
  if (label && pages[idx]) {
    label.textContent = `Page ${pages[idx].num} of ${pages.length}`;
  }
}

function syncFloatingPage() {
  const scroll = document.getElementById('floating-viewer-scroll');
  const sheets = [...document.querySelectorAll('#floating-page-stack figure')];
  if (!scroll || !sheets.length) return;
  
  const firstOffset = sheets[0].offsetTop;
  const marker = scroll.scrollTop + 120;
  let activeIdx = 0;
  
  sheets.forEach((sheet, idx) => {
    if (sheet.offsetTop - firstOffset <= marker) activeIdx = idx;
  });
  
  // Update thumbnail highlight
  document.querySelectorAll('.floating-thumb').forEach((el, i) => {
    if (i === activeIdx) {
      el.style.background = 'var(--orange-light)';
      el.style.borderLeft = '3px solid var(--orange)';
    } else {
      el.style.background = '';
      el.style.borderLeft = '';
    }
  });
  
  const pages = window._floatingPages || [];
  const label = document.getElementById('floating-current-page');
  if (label && pages[activeIdx]) {
    label.textContent = `Page ${pages[activeIdx].num} of ${pages.length}`;
  }
}

function adjustFloatingZoom(delta) {
  const stack = document.getElementById('floating-page-stack');
  const label = document.getElementById('floating-zoom-level');
  if (!stack) return;
  
  if (delta === 0) {
    window._floatingZoom = 100;
  } else {
    window._floatingZoom = Math.min(300, Math.max(25, window._floatingZoom + delta));
  }
  
  stack.style.width = window._floatingZoom + '%';
  if (label) label.textContent = window._floatingZoom + '%';
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

function _normalizeFamilyVariantJson(value) {
  if (!value) return {};
  if (typeof value === 'object' && !Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (error) {
    return {};
  }
}

function _normalizeFamilyVariant(variant = {}) {
  return {
    variant_id: String(variant.variant_id || variant.id || '').trim(),
    name: String(variant.name || '').trim(),
    product_code: String(variant.product_code || '').trim(),
    order_number: String(variant.order_number || '').trim(),
    size: String(variant.size || '').trim(),
    unit: String(variant.unit || '').trim(),
    specifications: _normalizeFamilyVariantJson(variant.specifications),
    ordering_data: _normalizeFamilyVariantJson(variant.ordering_data),
  };
}

function _familyVariantHasData(variant) {
  return Boolean(
    variant.variant_id ||
    variant.name ||
    variant.product_code ||
    variant.order_number ||
    variant.size ||
    variant.unit ||
    Object.keys(variant.specifications || {}).length ||
    Object.keys(variant.ordering_data || {}).length
  );
}

function _collectFamilyVariants() {
  return [...document.querySelectorAll('#family-variants-list .family-variant-row')]
    .map(row => _normalizeFamilyVariant({
      variant_id: row.dataset.variantId || '',
      name: row.querySelector('[data-variant-field="name"]')?.value || '',
      product_code: row.querySelector('[data-variant-field="product_code"]')?.value || '',
      order_number: row.querySelector('[data-variant-field="order_number"]')?.value || '',
      size: row.querySelector('[data-variant-field="size"]')?.value || '',
      unit: row.querySelector('[data-variant-field="unit"]')?.value || '',
      specifications: _normalizeFamilyVariantJson(row.querySelector('[data-variant-field="specifications"]')?.value || '{}'),
      ordering_data: _normalizeFamilyVariantJson(row.querySelector('[data-variant-field="ordering_data"]')?.value || '{}'),
    }))
    .filter(_familyVariantHasData);
}

function renderFamilyVariantRows(variants = []) {
  const list = document.getElementById('family-variants-list');
  if (!list) return;
  const rows = (variants || []).map(_normalizeFamilyVariant);
  if (!rows.length) rows.push(_normalizeFamilyVariant());
  list.innerHTML = rows.map((variant, index) => `
    <div class="family-variant-row" data-variant-index="${index}" data-variant-id="${escAttr(variant.variant_id)}">
      <div class="family-variant-grid">
        <input type="text" data-variant-field="name" placeholder="Variant name" value="${escAttr(variant.name)}" />
        <!-- Keep legacy structured fields hidden so existing data round-trips without loss. -->
        <input type="hidden" data-variant-field="product_code" value="${escAttr(variant.product_code)}" />
        <input type="hidden" data-variant-field="order_number" value="${escAttr(variant.order_number)}" />
        <input type="hidden" data-variant-field="size" value="${escAttr(variant.size)}" />
        <input type="hidden" data-variant-field="unit" value="${escAttr(variant.unit)}" />
      </div>
      <input type="hidden" data-variant-field="specifications" value="${escAttr(JSON.stringify(variant.specifications || {}))}" />
      <input type="hidden" data-variant-field="ordering_data" value="${escAttr(JSON.stringify(variant.ordering_data || {}))}" />
      <button type="button" class="btn btn-sm btn-secondary family-variant-remove" onclick="removeFamilyVariantRow(${index})">
        <i class="fa fa-times"></i>
      </button>
    </div>
  `).join('');
}

function addFamilyVariantRow() {
  renderFamilyVariantRows([..._collectFamilyVariants(), _normalizeFamilyVariant()]);
}

function removeFamilyVariantRow(index) {
  const variants = _collectFamilyVariants();
  variants.splice(index, 1);
  renderFamilyVariantRows(variants);
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
  const productCode = document.getElementById('family-product-code');
  const category = document.getElementById('family-category');
  const aliases = document.getElementById('family-aliases');
  const status = document.getElementById('family-review-status');
  if (id) id.value = '';
  if (name) name.value = '';
  if (productCode) productCode.value = '';
  if (category) category.value = '';
  if (aliases) aliases.value = '';
  if (status) status.value = 'approved';
  renderFamilyVariantRows([]);
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
    bits.push(`Current product: <strong>${escHtml(familyNames[0])}</strong>`);
  } else if (familyNames.length > 1) {
    bits.push(`<strong>${familyNames.length}</strong> assigned products`);
  } else {
    bits.push('Unassigned chunks');
  }
  if (pageStart && pageEnd) {
    bits.push(pageStart === pageEnd ? `Page ${pageStart}` : `Pages ${pageStart}-${pageEnd}`);
  }

  summary.innerHTML = bits.join(' &middot; ');
}

const _familyPageState = { page: 1, pageSize: 50 };

function toggleSelectAllFamilyChunks(checked) {
  const chunks = _familyPanelState.chunks || [];
  const q = (_familyPanelState.search || '').toLowerCase();
  const visible = q ? chunks.filter(c => [
    c.id, c.filename, c.product_name, c.family_name, c.family_code, c.family_status, c.excerpt
  ].join(' ').toLowerCase().includes(q)) : chunks;
  visible.forEach(c => {
    if (checked) _familySelectedChunkIds.add(c.id);
    else _familySelectedChunkIds.delete(c.id);
  });
  renderFamilyChunks();
  _updateFamilySelectionUI();
}

function _updateFamilySelectionUI() {
  const count = _familySelectedChunkIds.size;
  const countEl = document.getElementById('family-selected-count');
  if (countEl) countEl.textContent = count > 0 ? `${count} selected` : '';
  const badge = document.getElementById('family-editor-chunk-count');
  if (badge) badge.textContent = `${count} selected`;
  // Sync select-all checkbox state
  const chunks = _familyPanelState.chunks || [];
  const q = (_familyPanelState.search || '').toLowerCase();
  const visible = q ? chunks.filter(c => [
    c.id, c.filename, c.product_name, c.family_name, c.family_code, c.family_status, c.excerpt
  ].join(' ').toLowerCase().includes(q)) : chunks;
  const allChecked = visible.length > 0 && visible.every(c => _familySelectedChunkIds.has(c.id));
  const cb = document.getElementById('family-select-all');
  if (cb) { cb.checked = allChecked; cb.indeterminate = !allChecked && count > 0; }
}

function _renderFamilyPagination(total, filtered) {
  const el = document.getElementById('family-pagination');
  if (!el) return;
  const { page, pageSize } = _familyPageState;
  const totalPages = Math.ceil(filtered / pageSize);
  if (totalPages <= 1) { el.innerHTML = `<span>${filtered} of ${total} chunks</span>`; return; }
  const start = (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, filtered);
  el.innerHTML = `
    <span>${start}–${end} of ${filtered} chunks${filtered < total ? ` (filtered from ${total})` : ''}</span>
    <div style="display:flex;gap:4px;align-items:center">
      <button class="btn btn-sm btn-secondary" onclick="_familyGoPage(1)" ${page===1?'disabled':''} title="First">&laquo;</button>
      <button class="btn btn-sm btn-secondary" onclick="_familyGoPage(${page-1})" ${page===1?'disabled':''} title="Prev">&lsaquo;</button>
      <span style="padding:0 8px;font-weight:600">${page} / ${totalPages}</span>
      <button class="btn btn-sm btn-secondary" onclick="_familyGoPage(${page+1})" ${page===totalPages?'disabled':''} title="Next">&rsaquo;</button>
      <button class="btn btn-sm btn-secondary" onclick="_familyGoPage(${totalPages})" ${page===totalPages?'disabled':''} title="Last">&raquo;</button>
      <select onchange="_familySetPageSize(+this.value)" style="font-size:12px;padding:2px 4px;border:1px solid #e2e8f0;border-radius:4px">
        ${[25,50,100,200].map(n => `<option value="${n}" ${n===pageSize?'selected':''}>${n} / page</option>`).join('')}
      </select>
    </div>`;
}

function _familyGoPage(p) {
  const chunks = _familyPanelState.chunks || [];
  const q = (_familyPanelState.search || '').toLowerCase();
  const filtered = q ? chunks.filter(c => [
    c.id, c.filename, c.product_name, c.family_name, c.family_code, c.family_status, c.excerpt
  ].join(' ').toLowerCase().includes(q)) : chunks;
  const totalPages = Math.ceil(filtered.length / _familyPageState.pageSize) || 1;
  _familyPageState.page = Math.max(1, Math.min(p, totalPages));
  renderFamilyChunks();
}

function _familySetPageSize(n) {
  _familyPageState.pageSize = n;
  _familyPageState.page = 1;
  renderFamilyChunks();
}

function renderFamilyChunks() {
  const body = document.getElementById('family-chunks-table-body');
  if (!body) return;

  const allChunks = _familyPanelState.chunks || [];
  if (!allChunks.length) {
    body.innerHTML = `
      <tr>
        <td colspan="8" class="index-table-empty">
          <i class="fa fa-sitemap" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
          No chunks found for this PDF.
        </td>
      </tr>
    `;
    _renderFamilyPagination(0, 0);
    return;
  }

  const q = (_familyPanelState.search || '').toLowerCase();
  const filtered = q ? allChunks.filter(c => [
    c.id, c.filename, c.product_name, c.family_name, c.family_code, c.family_status, c.excerpt, c.page_start, c.page_end
  ].join(' ').toLowerCase().includes(q)) : allChunks;

  const { page, pageSize } = _familyPageState;
  const start = (page - 1) * pageSize;
  const chunks = filtered.slice(start, start + pageSize);

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
        <td style="text-align:center;">
          <button class="btn btn-sm btn-secondary" style="padding:3px 10px; font-size:12px;" onclick='event.stopPropagation(); showFamilyChunkPreview(${JSON.stringify(chunk.id)})'>
            <i class="fa fa-eye"></i> Chunk
          </button>
        </td>
        <td class="family-status">
          ${chunk.status === 'Embedded'
            ? '<span class="badge badge-green"><i class="fa fa-database"></i> Embedded</span>'
            : '<span class="badge badge-grey"><i class="fa fa-check-circle"></i> Ready</span>'}
        </td>
      </tr>
    `;
  }).join('');

  _renderFamilyPagination(allChunks.length, filtered.length);
  _updateFamilySelectionUI();
  handleFamilySearch();
}

function showFamilyChunkPreview(chunkId) {
  const chunk = _familyPanelState.chunksById[String(chunkId)];
  if (!chunk) return;
  const modal = document.getElementById('family-chunk-modal');
  document.getElementById('family-chunk-modal-title').textContent =
    `${chunk.filename || chunkId} — ${chunk.product_name || ''}`;
  document.getElementById('family-chunk-modal-body').innerHTML =
    mdToHtml(chunk.content || chunk.excerpt || '(no content)');
  modal.style.display = 'flex';
}

function closeFamilyChunkModal() {
  document.getElementById('family-chunk-modal').style.display = 'none';
}

function renderFamilyCards() {
  const list = document.getElementById('family-card-list');
  const badge = document.getElementById('family-count-badge');
  if (!list) return;

  const families = _familyPanelState.families || [];
  const q = (_familyPanelState.search || '').toLowerCase();
  const filtered = q ? families.filter(f =>
    (f.product_name || '').toLowerCase().includes(q) ||
    (f.category || '').toLowerCase().includes(q) ||
    (f.product_code || '').toLowerCase().includes(q)
  ) : families;

  if (badge) badge.textContent = String(filtered.length);

  const unassigned = (_familyPanelState.chunks || []).filter(c => !c.family_id);
  const unassignedCard = unassigned.length ? `
    <div class="family-card${_familySelectedId === '__unassigned__' ? ' active' : ''}" onclick="loadUnassignedCard()" style="border-left: 3px solid #94a3b8;">
      <div class="family-card-top">
        <div>
          <div class="family-card-title" style="color:#64748b;">Unassigned</div>
          <div class="family-card-sub">Chunks not yet assigned to a product</div>
        </div>
        <span class="family-badge muted">Unassigned</span>
      </div>
      <div class="family-card-meta">
        <span class="badge badge-grey">${unassigned.length} chunk${unassigned.length === 1 ? '' : 's'}</span>
        <span class="badge badge-grey">${unassigned.slice(0,3).map(c => `C${String(c.ordinal||0).padStart(3,'0')}`).join(', ')}${unassigned.length > 3 ? ` +${unassigned.length-3} more` : ''}</span>
      </div>
    </div>
  ` : '';

  const emptyMessage = q
    ? `<div class="family-card-empty">No products match "${escHtml(q)}".</div>`
    : '<div class="family-card-empty">No products created yet for this PDF.</div>';

  const familyCards = filtered.map(family => {
    const active = _familySelectedId === family.id;
    const chunkPreview = (family.chunks || []).slice(0, 3).map(chunk => `C${String(chunk.ordinal || 0).padStart(3, '0')}`).join(', ');
    const more = family.chunk_count > 3 ? ` +${family.chunk_count - 3} more` : '';
    const variantCount = (family.variants || []).length;
    const aliasCount = (family.aliases || []).length;
    const pageText = family.page_start && family.page_end
      ? (family.page_start === family.page_end ? `Page ${family.page_start}` : `Pages ${family.page_start}-${family.page_end}`)
      : 'Page range unset';

    return `
      <div class="family-card${active ? ' active' : ''}" onclick='loadFamilyFromCard(${JSON.stringify(family.id)})'>
        <div class="family-card-top">
          <div>
            <div class="family-card-title">${escHtml(family.product_name || 'Unnamed product')}</div>
            <div class="family-card-sub">${escHtml(family.category || 'Uncategorized')} · ${escHtml(pageText)}</div>
          </div>
          ${_familyStatusBadge(family.review_status)}
        </div>
        <div class="family-card-meta">
          <span class="badge badge-blue">${family.chunk_count} chunk${family.chunk_count === 1 ? '' : 's'}</span>
          ${family.product_code ? `<span class="badge badge-grey">${escHtml(family.product_code)}</span>` : ''}
          ${variantCount ? `<span class="badge badge-grey">${variantCount} variant${variantCount === 1 ? '' : 's'}</span>` : ''}
          ${aliasCount ? `<span class="badge badge-grey">${aliasCount} alias${aliasCount === 1 ? '' : 'es'}</span>` : ''}
          ${chunkPreview ? `<span class="badge badge-grey">${escHtml(chunkPreview)}${escHtml(more)}</span>` : ''}
        </div>
      </div>
    `;
  }).join('');

  if (!familyCards && !unassignedCard) {
    list.innerHTML = emptyMessage;
    return;
  }

  list.innerHTML = familyCards || emptyMessage;
  if (unassignedCard) {
    list.innerHTML += unassignedCard;
  }
}

function loadUnassignedCard() {
  _familySelectedId = '__unassigned__';
  const unassigned = (_familyPanelState.chunks || []).filter(c => !c.family_id);
  _familySelectedChunkIds = new Set(unassigned.map(c => String(c.id)));
  _familyResetForm();
  renderFamilyCards();
  renderFamilyChunks();
  renderFamilySelectionSummary();
}

function renderFamilyWorkspace() {
  const familyPdf = document.getElementById('family-panel-pdf');
  if (familyPdf) {
    familyPdf.textContent = _familyPanelState.pdf || 'Select a chunked PDF to review products';
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
  _updateFamilySelectionUI();
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
  const productCode = document.getElementById('family-product-code');
  const category = document.getElementById('family-category');
  const aliases = document.getElementById('family-aliases');
  const status = document.getElementById('family-review-status');
  if (id) id.value = family.id;
  if (name) name.value = family.product_name || '';
  if (productCode) productCode.value = family.product_code || '';
  if (category) category.value = family.raw_category || '';
  if (aliases) aliases.value = (family.aliases || []).join(', ');
  if (status) status.value = family.review_status || 'approved';
  renderFamilyVariantRows(family.variants || []);

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
  if (q !== _familyPanelState.search) {
    _familyPanelState.search = q;
    _familyPageState.page = 1;
    renderFamilyChunks();
  }
  renderFamilyCards();
}

async function loadFamilyPanel(force = false) {
  if (!IS_ADMIN) return;
  const body = document.getElementById('family-chunks-table-body');
  const cardList = document.getElementById('family-card-list');
  if (!body || !cardList) return;

  try {
    if (!_pdfDetails.length || force) {
      await ensurePdfDetails();
    }

    // Use the PDF shown in progress bar (parent stem for split PDFs)
    // selectedPdf is set directly by openFamilies — prefer it over _trackedPdf
    let pdfName = selectedPdf || _trackedPdf;
    
    // If pdfName doesn't have .pdf extension, add it for lookup
    if (pdfName && !pdfName.toLowerCase().endsWith('.pdf')) {
      pdfName = pdfName + '.pdf';
    }
    
    let detail = _findPdfDetail(pdfName);
    
    // If not found, check if it's a parent stem of split PDFs
    if (!detail || !detail.chunks_count) {
      const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
      let splitPart = null;
      if (pdfName) {
        const stem = pdfName.replace(/\.pdf$/i, '');
        splitPart = _pdfDetails.find(d => splitRe.test(d.name) && _rootStem(d.name) === stem);
      }
      if (splitPart && splitPart.chunks_count > 0) {
        pdfName = splitPart.name;
        detail = splitPart;
      } else {
        // Fall back to default chunked PDF
        detail = _defaultChunkedPdf();
        if (detail) {
          pdfName = detail.name;
          selectedPdf = pdfName;
          _highlightPdfRow(pdfName);
        }
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

    // Backend expects the stem (without .pdf) to resolve the document
    const apiPdfParam = pdfName.replace(/\.pdf$/i, '');
    const res = await fetch(`/admin-panel/api/families/?pdf=${encodeURIComponent(apiPdfParam)}`);
    const data = await res.json();
    if (!res.ok || data.error) {
      throw new Error(data.error || 'Could not load products.');
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
      const productCode = document.getElementById('family-product-code');
      const category = document.getElementById('family-category');
      const aliases = document.getElementById('family-aliases');
      const status = document.getElementById('family-review-status');
      if (id) id.value = family.id;
      if (name) name.value = family.product_name || '';
      if (productCode) productCode.value = family.product_code || '';
      if (category) category.value = family.raw_category || '';
      if (aliases) aliases.value = (family.aliases || []).join(', ');
      if (status) status.value = family.review_status || 'approved';
      renderFamilyVariantRows(family.variants || []);
    } else {
      const validChunkIds = new Set(Object.keys(_familyPanelState.chunksById));
      _familySelectedChunkIds = new Set([..._familySelectedChunkIds].filter(id => validChunkIds.has(String(id))));
    }

    renderFamilyWorkspace();

    // Auto-approve: if no families exist yet, auto-save each unassigned chunk as an approved family
    const unassigned = (nextState.chunks || []).filter(c => !c.family_id);
    if (!nextState.families.length && unassigned.length) {
      await _autoApproveFamilies(nextState.pdf, unassigned);
    }
  } catch (error) {
    body.innerHTML = `
      <tr>
        <td colspan="8" class="index-table-empty" style="color:var(--orange)">
          <i class="fa fa-exclamation-triangle" style="font-size: 32px; display: block; margin-bottom: 10px;"></i>
          ${escHtml(error.message)}
        </td>
      </tr>
    `;
    cardList.innerHTML = '<div class="family-card-empty">Failed to load products.</div>';
    showToast(error.message, 'error');
  }
}

async function saveProductFamily() {
  const pdfName = _familyPanelState.pdf || selectedPdf || '';
  const familyId = (document.getElementById('family-id')?.value || _familySelectedId || '').trim();
  const productName = document.getElementById('family-name')?.value.trim() || '';
  const productCode = document.getElementById('family-product-code')?.value.trim() || '';
  const rawCategory = document.getElementById('family-category')?.value.trim() || '';
  const aliases = document.getElementById('family-aliases')?.value.trim() || '';
  const variants = _collectFamilyVariants();
  const reviewStatus = document.getElementById('family-review-status')?.value || 'approved';
  const chunkIds = [..._familySelectedChunkIds];

  if (!pdfName) {
    showToast('Select a chunked PDF first.', 'error');
    return;
  }
  if (!productName) {
    showToast('Product name is required.', 'error');
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
        aliases,
        variants,
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
    const productCodeField = document.getElementById('family-product-code');
    const category = document.getElementById('family-category');
    const aliasesField = document.getElementById('family-aliases');
    const status = document.getElementById('family-review-status');
    if (id) id.value = _familySelectedId;
    if (name) name.value = data.family?.product_name || productName;
    if (productCodeField) productCodeField.value = data.family?.product_code || productCode;
    if (category) category.value = data.family?.raw_category || rawCategory;
    if (aliasesField) aliasesField.value = (data.family?.aliases || []).join(', ') || aliases;
    if (status) status.value = data.family?.review_status || reviewStatus;
    renderFamilyVariantRows(data.family?.variants || variants);

    renderFamilyWorkspace();
    document.getElementById('next-to-index-families').style.display = 'flex';
    markStepDone('families');
    
    // Update progress based on approval ratio
    if (data.progress_info) {
      const { total_families, approved_families, progress_percent } = data.progress_info;
      const displayPdf = _trackedPdf || pdfName.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || pdfName;
      
      // Update local state
      _trackedPdf = displayPdf;
      _trackedPercent = progress_percent;
      _trackedSetAt = Date.now();
      
      // Determine stage: stay at 'chunked' until all reviewed products are approved
      if (approved_families === total_families && total_families > 0) {
        _trackedStage = 'families';
        _trackedPercent = null; // Use stage default of 60%
        _renderProgress(displayPdf, 'families');
        // Update DB stage to families (60%)
        fetch('/admin-panel/api/update-pdf-stage/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
          body: JSON.stringify({ filename: pdfName, stage: 'families' })
        }).catch(() => {});
      } else {
        _trackedStage = 'chunked';
        _renderProgress(displayPdf, 'chunked', progress_percent);
        const sub = `${approved_families} of ${total_families} products approved. Approve all products to advance to 60%.`;
        document.getElementById('workflow-progress-sub').textContent = sub;
      }
    } else {
      advanceTrackedStage('families', pdfName);
    }
    
    refreshStats();
    loadPdfList();
    showToast(data.message || 'Product saved.', 'success');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = '<i class="fa fa-save"></i> Save Product';
    }
  }
}

// Minimal markdown → HTML for chunk display
function mdToHtml(md) {
  if (!md) return '';

  // Strip image references before escaping
  let src = md.replace(/!\[[^\]]*\]\([^)]*\)/g, '');

  // Normalise ALL-CAPS headings (e.g. "2 SPEED DESIGN", "# SPECIFICATIONS") → proper headings
  src = src.replace(/^(#+)\s+([A-Z][A-Z\d ()&/:,-]{3,})$/gm, (_, hashes, text) => {
    const titled = text.replace(/\b([A-Z]{2,})\b/g, w => w[0] + w.slice(1).toLowerCase());
    return `${hashes} ${titled}`;
  });
  src = src.replace(/^([\d]*[\d.]?\s*[A-Z][A-Z\d ()&/:,-]{3,})$/gm, line => {
    const trimmed = line.trim();
    const titled = trimmed.replace(/\b([A-Z]{2,})\b/g, w => w[0] + w.slice(1).toLowerCase());
    return `## ${titled}`;
  });

  let h = escHtml(src);

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
  h = h.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  h = h.replace(/^##### (.+)$/gm, '<h5>$1</h5>');

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
const _indexPageState = { page: 1, pageSize: 20 };
let _indexCurrentChunks = [];

function _indexSetPageSize(n) {
  _indexPageState.pageSize = n;
  _indexPageState.page = 1;
  const q = (document.getElementById('index-chunk-search')?.value || '').toLowerCase().trim();
  const base = _cachedIndexChunks.filter(c => c.status === 'Embedded');
  const filtered = q ? base.filter(c => {
    const id = `C${String(c.ordinal || 0).padStart(3, '0')}`.toLowerCase();
    return id.includes(q) || (c.product_name || '').toLowerCase().includes(q) || (c.content || '').toLowerCase().includes(q);
  }) : base;
  renderIndexChunks(filtered);
}

async function loadIndexPanel() {
  const pdfTitleEl = document.getElementById('index-selected-pdf');
  const tableBody = document.getElementById('index-chunks-table-body');
  if (!pdfTitleEl || !tableBody) return;

  // 1. Ensure _pdfDetails is loaded, then resolve selectedPdf
  try {
    await ensurePdfDetails();
  } catch (e) {
    console.error('Failed to load PDF list in index panel', e);
  }
  const _splitPartRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  function _isParentStem(name) {
    if (!name || _splitPartRe.test(name)) return false;
    const stem = name.replace(/\.pdf$/i, '');
    return _pdfDetails.some(d => _splitPartRe.test(d.name) && d.name.replace(_splitPartRe, '') === stem);
  }
  // Always prefer the progress-bar tracked PDF — it reflects what the user is actively working on
  if (_trackedPdf) {
    const trackedStem = _trackedPdf.replace(/\.pdf$/i, '');
    const trackedWithExt = trackedStem + '.pdf';
    // Check if this is a parent stem that has split children
    const hasChildren = _pdfDetails.some(d => _splitPartRe.test(d.name) && _rootStem(d.name) === trackedStem);
    if (hasChildren) {
      // Use parent stem — backend aggregates all split parts
      selectedPdf = trackedWithExt;
    } else {
      const trackedDetail = _findPdfDetail(trackedWithExt) || _findPdfDetail(_trackedPdf);
      if (trackedDetail) {
        selectedPdf = trackedDetail.name;
        _highlightPdfRow(selectedPdf);
      } else {
        selectedPdf = trackedWithExt;
      }
    }
  } else if (!selectedPdf || (!_isParentStem(selectedPdf) && !_findPdfDetail(selectedPdf)?.chunks_count)) {
    const pdfs = _pdfDetails || [];
    const picked = _findPdfDetail(selectedPdf) || _defaultChunkedPdf() || pdfs[0];
    if (picked) {
      selectedPdf = picked.name;
      _highlightPdfRow(selectedPdf);
    }
  }
  
  // Auto-detect and update progress when navigating to index panel
  if (selectedPdf) {
    try {
      const statsRes = await fetch('/admin-panel/api/stats/');
      const statsData = await statsRes.json();
      const currentStage = (statsData.pdf_progress || {})[selectedPdf];
      
      // Only auto-update to indexed if currently at chunked stage
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
        }
      }
      
      // Now update the progress bar with the correct stage from server
      if (currentStage && !_trackedPdf) {
        const displayPdf = selectedPdf.replace(/_(custom_)?p\d{4}-\d{4}\.pdf$/i, '') || selectedPdf;
        setTrackedPdf(displayPdf, currentStage);
      }

      // Pin _userSelectedPdf to the resolved PDF so refreshStats() can't overwrite
      // the progress bar with a different server-tracked PDF (e.g. Fluid_Handling...)
      if (_trackedPdf && !_userSelectedPdf) {
        _userSelectedPdf = _trackedPdf;
      }

      await refreshStats();
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

  // 3. Determine the lookup PDF for the chunks API.
  //    If selectedPdf is a parent stem (has split children), pass the stem so the backend aggregates all parts.
  //    If selectedPdf is a split part, use its root stem to aggregate all siblings.
  //    Otherwise use the exact file.
  const splitRe = /_(custom_)?p\d{4}-\d{4}\.pdf$/i;
  const selectedStem = selectedPdf.replace(/\.pdf$/i, '');
  const isParentWithChildren = !splitRe.test(selectedPdf) &&
    _pdfDetails.some(d => _splitPartRe.test(d.name) && _rootStem(d.name) === selectedStem);
  const isSplitPart = splitRe.test(selectedPdf);
  const chunksPdf = isParentWithChildren ? selectedStem
    : isSplitPart ? _rootStem(selectedPdf)
    : selectedPdf;
  const displayName = isParentWithChildren ? selectedStem
    : isSplitPart ? _rootStem(selectedPdf)
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
    let res = await fetch(`/admin-panel/api/chunks/?pdf=${encodeURIComponent(chunksPdf)}&index=1`);
    let data = await res.json();
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
      _indexPageState.page = 1;
      const embeddedChunks = _cachedIndexChunks.filter(c => c.status === 'Embedded');
      renderIndexChunks(embeddedChunks);
      // Show approve button immediately if there are embedded chunks (no query required)
      if (embeddedChunks.some(c => c.status === 'Embedded')) {
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

  if (!chunks.length) {
    tableBody.innerHTML = `
      <tr>
        <td colspan="4" class="index-table-empty">
          <i class="fa fa-search" style="font-size: 32px; display: block; margin-bottom: 10px; color: #ccc;"></i>
          No matching chunks found.
        </td>
      </tr>
    `;
    _renderPagination('index-pagination', 1, 1, () => {}, _indexPageState.pageSize, '_indexSetPageSize');
    return;
  }

  const totalPages = Math.max(1, Math.ceil(chunks.length / _indexPageState.pageSize));
  _indexPageState.page = Math.min(_indexPageState.page, totalPages);
  const start = (_indexPageState.page - 1) * _indexPageState.pageSize;
  const pageChunks = chunks.slice(start, start + _indexPageState.pageSize);

  tableBody.innerHTML = '';
  pageChunks.forEach(c => {
    const tr = document.createElement('tr');
    const chunkId = `C${String(c.ordinal || 0).padStart(3, '0')}`;
    let cleanText = String(c.content || '').trim().replace(/\s+/g, ' ');
    const excerpt = cleanText.length > 90 ? `...${cleanText.substring(0, 90)}...` : cleanText.length > 0 ? `...${cleanText}...` : '';
    const badgeHtml = c.status === 'Embedded'
      ? `<span class="badge-status-embedded"><i class="fa fa-database"></i> Embedded</span>`
      : `<span class="badge-status-ready"><i class="fa fa-check-circle"></i> Ready</span>`;
    tr.innerHTML = `
      <td style="font-weight:700;color:var(--dark);font-family:monospace">${escHtml(chunkId)}</td>
      <td style="font-weight:600;color:#475569">${escHtml(c.product_name || 'General Info')}</td>
      <td style="color:#64748b;font-style:italic">${escHtml(excerpt)}</td>
      <td style="text-align:center">${badgeHtml}</td>
    `;
    tableBody.appendChild(tr);
  });

  _indexCurrentChunks = chunks;
  _renderPagination('index-pagination', _indexPageState.page, totalPages,
    p => { _indexPageState.page = p; renderIndexChunks(_indexCurrentChunks); },
    _indexPageState.pageSize, '_indexSetPageSize');
}

function handleIndexSearch() {
  const searchInput = document.getElementById('index-chunk-search');
  if (!searchInput) return;
  const q = searchInput.value.toLowerCase().trim();
  _indexPageState.page = 1;

  if (!q) {
    renderIndexChunks(_cachedIndexChunks.filter(c => c.status === 'Embedded'));
    return;
  }

  const filtered = _cachedIndexChunks.filter(c => {
    if (c.status !== 'Embedded') return false;
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
    showToast(`"${pdfName}" testing disapproved. Progress reverted to 80%.`, 'info');

    // Re-render buttons: show clickable Approve button, hide Disapprove
    showMarkAsTestedButton();
  } catch (error) {
    showToast(error.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '<i class="fa fa-times-circle"></i> Disapprove Testing';
  }
}
