// ── Slider ────────────────────────────────────────────────────────────────────
const slides    = document.querySelectorAll('.h-slide');
const dotsWrap  = document.getElementById('slider-dots');
let current     = 0;
let autoTimer;

// Build dots
slides.forEach((_, i) => {
  const d = document.createElement('div');
  d.className = 'h-dot' + (i === 0 ? ' active' : '');
  d.onclick = () => goSlide(i);
  dotsWrap.appendChild(d);
});

function goSlide(idx) {
  slides[current].classList.remove('active');
  dotsWrap.children[current].classList.remove('active');
  current = (idx + slides.length) % slides.length;
  slides[current].classList.add('active');
  dotsWrap.children[current].classList.add('active');
}

function startAuto() {
  autoTimer = setInterval(() => goSlide(current + 1), 4000);
}
function stopAuto() { clearInterval(autoTimer); }

startAuto();
document.getElementById('slider').addEventListener('mouseenter', stopAuto);
document.getElementById('slider').addEventListener('mouseleave', startAuto);

// ── Chatbot Widget ────────────────────────────────────────────────────────────
let chatOpen = false;

function toggleChatbot() {
  chatOpen = !chatOpen;
  document.getElementById('chatbot-widget').classList.toggle('open', chatOpen);
  document.getElementById('chatbot-fab').classList.toggle('open', chatOpen);
  const icon = document.getElementById('fab-icon');
  icon.innerHTML = chatOpen
    ? '<i class="fa fa-times"></i>'
    : '<i class="fa fa-robot"></i>';
  if (chatOpen) document.getElementById('cw-input').focus();
}

function cwAskCat(cat, el) {
  document.querySelectorAll('.cw-cat').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  const q = cat === 'all'
    ? 'Show all available GROZ products'
    : `Show ${cat} products and their specifications`;
  document.getElementById('cw-input').value = q;
  cwSend();
}

async function cwSend() {
  const input = document.getElementById('cw-input');
  const q = input.value.trim();
  if (!q) return;

  cwAddMsg('user', escHtml(q));
  input.value = '';

  // Typing indicator
  const typing = document.createElement('div');
  typing.id = 'cw-typing';
  typing.className = 'cw-msg bot';
  typing.innerHTML = `<div class="cw-msg-avatar"><span>GZ</span></div>
    <div class="cw-msg-bubble"><div class="cw-typing"><span></span><span></span><span></span></div></div>`;
  const msgs = document.getElementById('cw-messages');
  msgs.appendChild(typing);
  msgs.scrollTop = msgs.scrollHeight;

  try {
    const csrf = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('csrftoken='))?.split('=')[1] || '';
    const res  = await fetch('/api/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify({ query: q })
    });
    const data = await res.json();
    document.getElementById('cw-typing')?.remove();
    if (data.error) {
      cwAddMsg('bot', `<span style="color:var(--orange)">${escHtml(data.error)}</span>`);
    } else {
      cwAddMsg('bot', cwRenderMd(data.answer));
    }
  } catch(e) {
    document.getElementById('cw-typing')?.remove();
    cwAddMsg('bot', 'Network error. Please try again.');
  }
}

function cwAddMsg(role, html) {
  const msgs = document.getElementById('cw-messages');
  const div  = document.createElement('div');
  div.className = `cw-msg ${role}`;
  div.innerHTML = `<div class="cw-msg-avatar">${role==='bot'?'<span>GZ</span>':'<i class="fa fa-user"></i>'}</div>
    <div class="cw-msg-bubble">${html}</div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

// Minimal markdown renderer for widget
function cwRenderMd(text) {
  if (!text) return '';
  let h = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h = h.replace(/\|(.+)\|\n\|[-| :]+\|\n((?:\|.+\|\n?)*)/g, (_,hdr,rows)=>{
    const ths = hdr.split('|').filter(s=>s.trim()).map(s=>`<th>${s.trim()}</th>`).join('');
    const trs = rows.trim().split('\n').map(r=>{
      const tds = r.split('|').filter(s=>s.trim()).map(s=>`<td>${s.trim()}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  });
  h = h.replace(/^### (.+)$/gm,'<strong>$1</strong><br>');
  h = h.replace(/^## (.+)$/gm,'<strong>$1</strong><br>');
  h = h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  h = h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h = h.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
  h = h.replace(/(<li>[\s\S]*?<\/li>)/g,m=>`<ul>${m}</ul>`);
  h = h.replace(/\n\n+/g,'<br><br>').replace(/\n/g,'<br>');
  return h;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
