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

  function inlineFormat(t) {
    t=t.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
    t=t.replace(/`([^`]+)`/g,'<code>$1</code>');
    return t;
  }

  function isTableSep(line) {
    return /^\s*\|[\s|:\-]+\|\s*$/.test(line);
  }

  function parseTable(lines, start) {
    // start = header row index, start+1 = separator row
    const headers = lines[start].split('|')
      .slice(1,-1).map(s=>`<th>${inlineFormat(s.trim())}</th>`).join('');
    let i = start + 2;
    const bodyRows = [];
    while (i < lines.length && /^\s*\|/.test(lines[i])) {
      const tds = lines[i].split('|')
        .slice(1,-1).map(s=>`<td>${inlineFormat(s.trim())}</td>`).join('');
      bodyRows.push(`<tr>${tds}</tr>`);
      i++;
    }
    return {
      html: `<table><thead><tr>${headers}</tr></thead><tbody>${bodyRows.join('')}</tbody></table>`,
      nextIndex: i
    };
  }

  const lines = text.split(/\r?\n/);
  // Group lines into blocks: tables vs text
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    if (/^\s*\|/.test(lines[i]) && i+1 < lines.length && isTableSep(lines[i+1])) {
      const {html, nextIndex} = parseTable(lines, i);
      blocks.push({type:'table', html});
      i = nextIndex;
    } else {
      if (!blocks.length || blocks[blocks.length-1].type !== 'text')
        blocks.push({type:'text', lines:[]});
      blocks[blocks.length-1].lines.push(lines[i]);
      i++;
    }
  }

  return blocks.map(b => {
    if (b.type === 'table') return b.html;
    let t = b.lines.join('\n');
    t=t.replace(/^#{4} (.+)$/gm,'<h4>$1</h4>');
    t=t.replace(/^#{3} (.+)$/gm,'<h3>$1</h3>');
    t=t.replace(/^#{2} (.+)$/gm,'<h3>$1</h3>');
    t=inlineFormat(t);
    t=t.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
    t=t.replace(/(<li>.*?<\/li>\n?)+/g,m=>`<ul>${m}</ul>`);
    t=t.replace(/\n{2,}/g,'</p><p>').replace(/\n/g,'<br>');
    t=t.replace(/^---+$/gm,'<hr>');
    return `<p>${t}</p>`;
  }).join('');
}
