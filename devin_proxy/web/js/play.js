// playground (via admin channel — no client key needed, still logged)
async function loadPlayground(){
  const d=await (await api('/admin/api/models')).json();
  const fams=d.families.map(f=>`<optgroup label="${esc(f.label)}">`
    +f.models.map(m=>`<option value="${esc(m.uid)}">${esc(m.label)} · ${esc(m.uid)}</option>`).join('')
    +'</optgroup>').join('');
  const al=Object.keys(d.aliases||{}).length
    ?'<optgroup label="别名">'+Object.entries(d.aliases).map(([a,t])=>`<option value="${esc(a)}">${esc(a)} → ${esc(t)}</option>`).join('')+'</optgroup>':'';
  $('#pg-model').innerHTML=(fams+al)||'<option value="default">default</option>';
  try{
    const ac=await (await api('/admin/api/accounts')).json();
    $('#pg-acct').innerHTML='<option value="">自动调度账号</option>'
      +(ac.accounts||[]).map(a=>`<option value="${a.id}" ${a.disabled?'disabled':''}>${esc(a.label)}${a.disabled?' (禁用)':''}</option>`).join('');
  }catch{}
}
let PG_HIST=[];   // [{role,content}] — conversation turns (system excluded)
function pgReset(){PG_HIST=[];$('#pg-out').innerHTML='<span class="muted">新对话已开始。</span>';$('#pg-meta').textContent='';$('#pg-in').value='';$('#pg-in').focus()}
function pgBubble(role,text,think){
  const d=document.createElement('div');d.className='msg '+role;
  d.innerHTML=`<div class="who">${role==='user'?'你':'助手'}</div>`;
  if(think){const t=document.createElement('div');t.className='think';t.textContent='[思考] '+think;d.appendChild(t)}
  d.appendChild(document.createTextNode(text));
  return d;
}
async function pgSend(){
  const btn=$('#pg-send');btn.disabled=true;$('#pg-meta').textContent='';
  const out=$('#pg-out');
  if(out.querySelector('.muted'))out.innerHTML='';   // clear placeholder
  const userText=$('#pg-in').value;
  PG_HIST.push({role:'user',content:userText});
  out.appendChild(pgBubble('user',userText));
  const asst=pgBubble('asst','');asst.lastChild.textContent='';out.appendChild(asst);
  const asstTxt=asst.lastChild;
  const msgs=[];
  if($('#pg-system').value.trim())msgs.push({role:'system',content:$('#pg-system').value});
  msgs.push(...PG_HIST);
  const stream=$('#pg-stream').checked;
  const t0=performance.now();
  const headers={'Content-Type':'application/json'};
  try{
    if(stream){
      const r=await fetch('/admin/api/playground',{method:'POST',headers,
        body:JSON.stringify({model:$('#pg-model').value,messages:msgs,stream:true,stream_options:{include_usage:true},account_id:$('#pg-acct').value||undefined})});
      if(r.status===401){location.href='/admin';throw new Error('unauthorized')}
      const rd=r.body.getReader(),dec=new TextDecoder();let buf='',think='',reply='';
      for(;;){
        const{done,value}=await rd.read();if(done)break;
        buf+=dec.decode(value,{stream:true});
        let i;while((i=buf.indexOf('\n\n'))>=0){
          const line=buf.slice(0,i);buf=buf.slice(i+2);
          if(!line.startsWith('data:'))continue;
          const data=line.slice(5).trim();if(data==='[DONE]')break;
          try{const j=JSON.parse(data);
            if(j.error){asstTxt.textContent+='[上游错误] '+j.error.message;continue}
            const d=j.choices?.[0]?.delta||{};
            if(d.reasoning_content){think+=d.reasoning_content;renderThink()}
            if(d.content){reply+=d.content;asstTxt.textContent+=d.content}
            if(j.usage)$('#pg-meta').textContent=`tokens: ${j.usage.prompt_tokens}+${j.usage.completion_tokens}`;
          }catch{}
        }
      }
      function renderThink(){let el=asst.querySelector('.think');if(!el){el=document.createElement('div');el.className='think';asst.insertBefore(el,asstTxt)}el.textContent='[思考] '+think}
      if(reply)PG_HIST.push({role:'assistant',content:reply});
    }else{
      const r=await fetch('/admin/api/playground',{method:'POST',headers,
        body:JSON.stringify({model:$('#pg-model').value,messages:msgs,account_id:$('#pg-acct').value||undefined})});
      const j=await r.json();
      const m=j.choices?.[0]?.message||{};
      if(m.reasoning_content){const t=document.createElement('div');t.className='think';t.textContent='[思考] '+m.reasoning_content;asst.insertBefore(t,asstTxt)}
      const reply=m.content||JSON.stringify(j,null,1);
      asstTxt.textContent=reply;
      if(m.content)PG_HIST.push({role:'assistant',content:m.content});
      if(j.usage)$('#pg-meta').textContent=`tokens: ${j.usage.prompt_tokens}+${j.usage.completion_tokens}`;
    }
    $('#pg-meta').textContent+=' · '+Math.round(performance.now()-t0)+'ms';
    $('#pg-in').value='';
    out.scrollTop=out.scrollHeight;
  }catch(e){asstTxt.textContent='错误: '+e.message}
  btn.disabled=false;
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')$('#modal').classList.remove('on');
  if(e.ctrlKey&&e.key==='Enter'&&$('#p-play').classList.contains('on'))pgSend();
});
