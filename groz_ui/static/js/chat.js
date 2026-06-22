function updateQuickChips(sources) {
  const names = sources.map(s => s.name).filter(n => n && n.trim());
  if (!names.length) return;
  const container = document.getElementById('quick-chips');
  if (!container) return;
  container.innerHTML = names.map(n =>
    `<span class="filter-chip" onclick="quickAsk(this)"><i class="fa fa-circle" style="font-size:7px"></i> ${n}</span>`
  ).join('');
}

function getCsrf(){
  return document.cookie.split(';').reduce((v,c)=>{
    const [k,val]=c.trim().split('=');
    return k==='csrftoken'?decodeURIComponent(val):v;
  },'');
}

const msgsEl = document.getElementById('messages');
const inputEl = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
let welcomed = false;

inputEl.addEventListener('keydown', e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();}
});
inputEl.addEventListener('input',()=>{
  inputEl.style.height='auto';
  inputEl.style.height=Math.min(inputEl.scrollHeight,120)+'px';
});

function removeWelcome(){
  if(!welcomed){ document.getElementById('welcome-state')?.remove(); welcomed=true; }
}

function quickAsk(el){ inputEl.value=el.textContent.trim(); sendMessage(); }
function quickAsk2(t){ inputEl.value=t; sendMessage(); }

function askCat(cat,el){
  document.querySelectorAll('.cat-item').forEach(i=>i.classList.remove('active'));
  el.classList.add('active');
  inputEl.value = cat==='all' ? 'Show all available products' : `Show ${cat} products and their specifications`;
  sendMessage();
}

function addMsg(role,html,sources=[]){
  removeWelcome();
  const d=document.createElement('div');
  d.className=`message ${role}`;
  const avatarHtml = role==='bot'
    ? `<div class="msg-avatar"><span>GZ</span></div>`
    : `<div class="msg-avatar"><i class="fa fa-user"></i></div>`;
  let srcHtml='';
  if(sources.length){
    srcHtml=`<div class="src-bar">${sources.map(s=>`<span class="src-tag"><i class="fa fa-tag"></i>${s.name||s.code}</span>`).join('')}</div>`;
  }
  d.innerHTML=`${avatarHtml}<div class="bubble"><div class="msg-content">${html}</div>${srcHtml}</div>`;
  msgsEl.appendChild(d);
  msgsEl.scrollTop=msgsEl.scrollHeight;
}

function addTyping(){
  removeWelcome();
  const d=document.createElement('div');
  d.className='message bot'; d.id='typing';
  d.innerHTML=`<div class="msg-avatar"><span>GZ</span></div>
    <div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
  msgsEl.appendChild(d);
  msgsEl.scrollTop=msgsEl.scrollHeight;
}

async function sendMessage(){
  const q=inputEl.value.trim();
  if(!q) return;
  addMsg('user',q.replace(/</g,'&lt;').replace(/\n/g,'<br>'));
  inputEl.value=''; inputEl.style.height='auto';
  sendBtn.disabled=true;
  addTyping();
  try{
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000); // 60s timeout
    const res=await fetch('/api/chat/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrf()},
      body:JSON.stringify({query:q}),
      signal: controller.signal
    });
    clearTimeout(timeout);
    const data=await res.json();
    document.getElementById('typing')?.remove();
    if(data.error){
      addMsg('bot',`<span style="color:var(--orange)"><i class="fa fa-exclamation-triangle"></i> ${data.error}</span>`);
    }else{
      addMsg('bot',renderMarkdown(data.answer),data.sources||[]);
      updateQuickChips(data.sources||[]);
    }
  }catch(e){
    document.getElementById('typing')?.remove();
    const msg = e.name === 'AbortError' ? 'Request timed out. Please try again.' : 'Network error. Please try again.';
    addMsg('bot',`<span style="color:var(--orange)"><i class="fa fa-exclamation-triangle"></i> ${msg}</span>`);
  }finally{
    sendBtn.disabled=false;
    inputEl.focus();
  }
}
