function showToast(msg, type='info', duration=4000) {
  const icons={success:'fa-check-circle',error:'fa-exclamation-circle',info:'fa-info-circle'};
  const t=document.createElement('div');
  t.className=`toast ${type}`;
  t.innerHTML=`<i class="fa ${icons[type]}"></i><span>${msg}</span>`;
  document.getElementById('toast-container').appendChild(t);
  setTimeout(()=>{t.style.animation='slideOut .3s ease forwards';setTimeout(()=>t.remove(),300);},duration);
}

function renderMarkdown(text) {
  if (!text) return '';
  text = String(text)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;');
  text = text.replace(/\|(.+)\|\n\|[-| :]+\|\n((?:\|.+\|\n?)*)/g,(_,header,rows)=>{
    const ths=header.split('|').filter(s=>s.trim()).map(s=>`<th>${s.trim()}</th>`).join('');
    const trs=rows.trim().split('\n').map(row=>{
      const tds=row.split('|').filter(s=>s.trim()).map(s=>`<td>${s.trim()}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  });
  text=text.replace(/^#### (.+)$/gm,'<h4>$1</h4>');
  text=text.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  text=text.replace(/^## (.+)$/gm,'<h3>$1</h3>');
  text=text.replace(/^# (.+)$/gm,'<h2>$1</h2>');
  text=text.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  text=text.replace(/`([^`]+)`/g,'<code>$1</code>');
  text=text.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
  text=text.replace(/(<li>[\s\S]*?<\/li>)/g,m=>`<ul>${m}</ul>`);
  text=text.replace(/\n\n+/g,'</p><p>').replace(/\n/g,'<br>');
  return `<p>${text}</p>`;
}
