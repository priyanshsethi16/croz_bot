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

function escapeHtml(value){
  return String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function addMsg(role,html,sources=[],meta=null){
  removeWelcome();
  const d=document.createElement('div');
  d.className=`message ${role}`;
  const avatarHtml = role==='bot'
    ? `<div class="msg-avatar"><span>GZ</span></div>`
    : `<div class="msg-avatar"><i class="fa fa-user"></i></div>`;
  let srcHtml='';
  if(sources.length){
    srcHtml=`<div class="src-bar">${sources.map(s=>{
      const label=escapeHtml(s.name||s.code||'Catalog source');
      const code=s.code?` · ${escapeHtml(s.code)}`:'';
      const pdf=s.pdf?` · ${escapeHtml(s.pdf)}`:'';
      const page=s.page_start?` · p.${escapeHtml(s.page_start)}${s.page_end&&s.page_end!==s.page_start?`–${escapeHtml(s.page_end)}`:''}`:'';
      return `<span class="src-tag"><i class="fa fa-tag"></i>${label}${code}${pdf}${page}</span>`;
    }).join('')}</div>`;
  }
  d.innerHTML=`${avatarHtml}<div class="bubble"><div class="msg-content">${html}</div>${srcHtml}</div>`;
  if(meta&&meta.total_results!==null&&meta.total_results!==undefined){
    const summary=document.createElement('div');
    summary.className='result-summary';
    summary.textContent=`${meta.total_results} result${meta.total_results===1?'':'s'}${meta.complete_result?' · complete':' · paginated/partial'}`;
    d.querySelector('.bubble').appendChild(summary);
    const shown=(meta.page||1)*(meta.page_size||50);
    if(!meta.complete_result&&shown<meta.total_results&&meta.query){
      const more=document.createElement('button');
      more.type='button';
      more.className='btn btn-sm btn-secondary';
      more.textContent='Next results';
      more.addEventListener('click',()=>sendMessage(meta.query,(meta.page||1)+1,false));
      d.querySelector('.bubble').appendChild(more);
    }
  }
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

async function sendMessage(queryOverride=null,page=1,showUser=true){
  const q=(queryOverride??inputEl.value).trim();
  if(!q) return;
  if(showUser) addMsg('user',escapeHtml(q).replace(/\n/g,'<br>'));
  inputEl.value=''; inputEl.style.height='auto';
  sendBtn.disabled=true;
  addTyping();
  try{
    const res=await fetch('/api/chat/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':CSRF},
      body:JSON.stringify({query:q,page:page,page_size:50})
    });
    const data=await res.json();
    document.getElementById('typing')?.remove();
    if(data.error){
      addMsg('bot',`<span style="color:var(--orange)"><i class="fa fa-exclamation-triangle"></i> ${data.error}</span>`);
    }else{
      addMsg('bot',renderMarkdown(data.answer),data.sources||[],{
        total_results:data.total_results,
        complete_result:data.complete_result,
        page:data.page||page,
        page_size:data.page_size||50,
        query:q
      });
    }
  }catch(e){
    document.getElementById('typing')?.remove();
    addMsg('bot','<span style="color:var(--orange)"><i class="fa fa-exclamation-triangle"></i> Network error. Please try again.</span>');
  }finally{
    sendBtn.disabled=false;
    inputEl.focus();
  }
}
