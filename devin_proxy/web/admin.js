const $ = s => document.querySelector(s);
const toast = m => { const t=$('#toast'); t.textContent=m; t.style.display='block'; clearTimeout(t._t); t._t=setTimeout(()=>t.style.display='none',3000) };
const fmt = n => n==null?'-':(n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n));
const fmtB = n => n>=1e6?(n/1e6).toFixed(1)+' MB':n>=1e3?(n/1e3).toFixed(1)+' KB':n+' B';
const fmtT = ts => ts?new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}):'-';
const fmtDur = s => s>86400?(s/86400).toFixed(1)+' 天':s>3600?(s/3600).toFixed(1)+' 小时':Math.round(s/60)+' 分钟';
const esc = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---------- auth (cookie session from /admin login) ----------
async function api(path, opts={}){
  const r = await fetch(path, opts);
  if (r.status === 401){ location.href='/admin'; throw new Error('unauthorized') }
  if (!r.ok){ let m; try{m=(await r.json()).detail}catch{} throw new Error(m||('HTTP '+r.status)) }
  return r;
}
async function logout(){
  await fetch('/admin/api/logout',{method:'POST'}).catch(()=>{});
  location.href='/admin';
}

// nav
document.querySelectorAll('.nav a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('on'));
  a.classList.add('on'); $('#p-'+a.dataset.p).classList.add('on');
  const fn=({dash:loadDash,reqs:loadReqs,accs:loadAccounts,models:loadModels,play:loadPlayground,keys:loadKeys,conf:loadConf})[a.dataset.p];
  fn().catch(e=>toast('加载失败: '+e.message));   // one bad page must not break nav
});

// ---------- dashboard ----------
let DASH={hours:24,data:null};
const SVGNS='http://www.w3.org/2000/svg';
const SE=(t,at)=>{const e=document.createElementNS(SVGNS,t);for(const k in at)e.setAttribute(k,at[k]);return e};
const niceMax=v=>{if(v<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(v)));const f=v/p;return (f<=1?1:f<=2?2:f<=2.5?2.5:f<=5?5:10)*p};
const p2=n=>String(n).padStart(2,'0');
const bLabel=(t,bucket)=>{const d=new Date(t*1000);
  if(bucket>=86400)return `${d.getMonth()+1}-${d.getDate()}`;
  return (DASH.hours>24?`${d.getMonth()+1}-${d.getDate()} `:'')+`${p2(d.getHours())}:${p2(d.getMinutes())}`};

document.querySelectorAll('#dash-range a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('#dash-range a').forEach(x=>x.classList.remove('on'));
  a.classList.add('on');DASH.hours=+a.dataset.h;loadDash();
});
function goPage(p){document.querySelector('[data-p='+p+']').click()}
function filterByModel(m){
  goPage('reqs');const sel=$('#rq-model');
  if(![...sel.options].some(o=>o.value===m))sel.appendChild(new Option(m,m));
  sel.value=m;reqReset();loadReqs();
}
function filterByAccount(a){goPage('reqs');reqAccount=a;reqReset();renderAcctChip();loadReqs()}

async function loadDash(){
  const d=await (await api('/admin/api/overview?hours='+DASH.hours)).json();
  DASH.data=d;
  $('#dash-time').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  renderDashCards(d);renderPool(d);renderReqChart(d);renderTokChart(d);
  renderModelShare(d);renderDashAccs(d);renderDashKeys(d);renderDashErrs(d);
  $('#foot').textContent=`账号 ${d.pool.ready||0}/${d.pool.total||0} 就绪 · 在途 ${d.pool.in_flight||0}`;
}
function renderDashCards(d){
  const rate=d.total?100*(d.total-d.errors)/d.total:null;
  const pool=d.pool||{};
  const disk=d.disk||{};
  const cards=[
    {k:'请求数',v:fmt(d.total),sub:`今日 ${fmt(d.today.requests)} · 累计 ${fmt(d.alltime.requests)}`},
    {k:'成功率',v:rate==null?'-':rate.toFixed(1)+'%',sub:`错误 ${d.errors}`,cls:rate==null?'':rate>=99?'green':rate>=95?'yellow':'red'},
    {k:'Tokens',v:fmt(d.input_tokens+d.output_tokens+(d.cached_tokens||0)),sub:`入 ${fmt(d.input_tokens)} · 出 ${fmt(d.output_tokens)} · 缓存 ${fmt(d.cached_tokens||0)}`},
    {k:'缓存命中',v:(d.cache_hit_pct??0)+'%',sub:`读 ${fmt(d.cached_tokens||0)} · 写 ${fmt(d.cache_creation_tokens||0)}`,cls:d.cache_hit_pct>=50?'green':d.cached_tokens?'yellow':''},
    {k:'平均延迟',v:d.avg_latency_ms+'ms',sub:`P50 ${d.p50_ms}ms · P95 ${d.p95_ms}ms`},
    {k:'平均 TPS',v:d.avg_tps||'-',sub:'tok/s 输出速率'},
    {k:'平均 TTFT',v:d.avg_ttft_ms?d.avg_ttft_ms+'ms':'-',sub:'流式占比 '+(d.total?Math.round(100*d.streams/d.total)+'%':'-')},
    {k:'每分钟请求',v:d.rpm,sub:`tok/min ${fmt(d.tpm)}`},
    {k:'在途请求',v:pool.in_flight||0,sub:`钉扎会话 ${pool.sessions||0}`},
    {k:'账号就绪',v:`${pool.ready||0}/${pool.total||0}`,sub:`冷却 ${pool.cooldown||0} · 启用 ${pool.enabled||0}`,cls:pool.total?(pool.ready?'green':'red'):'',click:'accs'},
    {k:'断流 · 重试',v:`${d.truncated||0} · ${d.retried||0}`,sub:'点击筛选断流日志',cls:d.truncated?'red':'',click:'trunc'},
    ...(disk.total?[{k:'磁盘剩余',v:fmtB(disk.free),sub:`${fmtB((disk.total||0)-(disk.free||0))} 已用 / ${fmtB(disk.total)}`,cls:disk.low?'red':disk.free/disk.total<0.15?'yellow':'',click:'conf'}]:[]),
  ];
  $('#cards').innerHTML=cards.map(c=>
    `<div class="card${c.click?' link':''}"${c.click?` onclick="cardGo('${c.click}')"`:''}><div class="k">${c.k}</div><div class="v ${c.cls}">${c.v}</div><div class="sub" title="${esc(c.sub)}">${esc(c.sub)}</div></div>`).join('');
}
function cardGo(k){
  if(k==='accs')return goPage('accs');
  if(k==='conf')return goPage('conf');
  if(k==='trunc'){goPage('reqs');$('#rq-flag').value='truncated';reqReset();loadReqs()}
}
function renderPool(d){
  const p=d.pool||{};
  $('#pool-sub').textContent=`共 ${p.total||0} 个 · 在途 ${p.in_flight||0} · 钉扎 ${p.sessions||0} · 运行 ${fmtDur(d.uptime_s||0)} · DB ${fmtB(d.db_size)}`;
  $('#pool-row').innerHTML=(p.accounts||[]).map(a=>{
    const cl=a.state==='ready'?'ok':a.state==='cooldown'?'err':'off';
    const txt=a.state==='ready'?'就绪':a.state==='cooldown'?`冷却 ${a.cooldown_s}s`:'禁用';
    return `<div class="acchip" onclick="goPage('accs')" title="${esc(a.label)}${a.plan?' · '+esc(a.plan):''}">
      <span class="adot ${cl}"></span><b>${esc(a.label)}</b>
      <span class="muted">${txt}</span>
      ${a.in_flight?`<span class="tag model">在途 ${a.in_flight}${a.max_concurrent?'/'+a.max_concurrent:''}</span>`:''}
      ${a.fails?`<span class="tag err">连败 ${a.fails}</span>`:''}</div>`;
  }).join('')||'<span class="muted">暂无账号 — 到「账号」页 OAuth 登录或手动添加</span>';
}
function chartTip(host,ev,html){
  const t=host.querySelector('.ctip')||host.appendChild(Object.assign(document.createElement('div'),{className:'ctip'}));
  t.innerHTML=html;t.style.display='block';
  const r=host.getBoundingClientRect();
  t.style.left=Math.max(2,Math.min(ev.clientX-r.left+12,r.width-t.offsetWidth-4))+'px';
  t.style.top=Math.max(2,ev.clientY-r.top-14)+'px';
}
const hideTip=host=>{const t=host.querySelector('.ctip');if(t)t.style.display='none'};

function renderReqChart(d){
  const el=$('#chart-req');el.innerHTML='';
  const s=d.series||[],n=s.length;
  const ep=(d.by_endpoint||[]).map(e=>`${esc(e.ep)} ${e.n}`).join(' · ');
  $('#chart-sub').textContent=(ep?'接口分布：'+ep:'')+` · 每桶 ${d.bucket_s>=3600?d.bucket_s/3600+'h':d.bucket_s/60+'min'}`;
  if(!n||!d.total){el.innerHTML='<div class="muted" style="padding:34px;text-align:center">该时间窗口内暂无请求</div>';return}
  const W=Math.max(360,el.clientWidth||800),H=230,pl=46,pr=48,pt=12,pb=24;
  const iw=W-pl-pr,ih=H-pt-pb;
  const svg=SE('svg',{width:'100%',height:H,viewBox:`0 0 ${W} ${H}`});
  const maxN=niceMax(Math.max(...s.map(b=>b.n)));
  const maxLat=niceMax(Math.max(10,...s.map(b=>b.avg_lat)));
  for(let i=0;i<=4;i++){
    const y=pt+ih-i*ih/4;
    svg.appendChild(SE('line',{x1:pl,y1:y,x2:W-pr,y2:y,stroke:'#21262d'}));
    const t=SE('text',{x:pl-6,y:y+3,'text-anchor':'end','font-size':10,fill:'#8b949e'});
    t.textContent=fmt(Math.round(maxN*i/4));svg.appendChild(t);
    const t2=SE('text',{x:W-pr+6,y:y+3,'font-size':10,fill:'#d29922'});
    if(i)t2.textContent=fmt(Math.round(maxLat*i/4));svg.appendChild(t2);
  }
  const step=iw/n,bw=Math.max(2,Math.min(step*0.68,56));
  const pts=[];
  s.forEach((b,i)=>{
    const cx=pl+i*step+step/2,x=cx-bw/2;
    const hOk=(b.n-b.errs)/maxN*ih,hE=b.errs/maxN*ih;
    if(hOk>0.4)svg.appendChild(SE('rect',{x,y:pt+ih-hOk,width:bw,height:hOk,fill:'#1f6feb',rx:1}));
    if(hE>0.4)svg.appendChild(SE('rect',{x,y:pt+ih-hOk-hE,width:bw,height:hE,fill:'#f85149',rx:1}));
    if(b.avg_lat>0)pts.push(cx+','+(pt+ih-b.avg_lat/maxLat*ih));
  });
  if(pts.length>1)svg.appendChild(SE('polyline',{points:pts.join(' '),fill:'none',stroke:'#d29922','stroke-width':1.5,'stroke-dasharray':'4 3',opacity:.9}));
  const tickN=Math.min(7,n);
  for(let i=0;i<tickN;i++){
    const idx=Math.round(i*(n-1)/(tickN-1||1));
    const t=SE('text',{x:pl+idx*step+step/2,y:H-7,'text-anchor':'middle','font-size':10,fill:'#8b949e'});
    t.textContent=bLabel(s[idx].t,d.bucket_s);svg.appendChild(t);
  }
  s.forEach((b,i)=>{
    const hz=SE('rect',{x:pl+i*step,y:pt,width:step,height:ih,fill:'transparent'});
    hz.onmousemove=ev=>chartTip(el,ev,
      `<b>${new Date(b.t*1000).toLocaleString('zh-CN',{hour12:false})}</b><br>${b.n} 请求 · ${b.errs} 错 · ${fmt(b.in_tok+b.out_tok)} tok · 均延 ${fmt(b.avg_lat)}ms`);
    hz.onmouseenter=ev=>ev.target.setAttribute('fill','#ffffff08');
    hz.onmouseleave=ev=>{ev.target.setAttribute('fill','transparent');hideTip(el)};
    svg.appendChild(hz);
  });
  el.appendChild(svg);
}
function renderTokChart(d){
  const el=$('#chart-tok');el.innerHTML='';
  const s=d.series||[],n=s.length;
  if(!n||!d.total){el.innerHTML='<div class="muted" style="padding:30px;text-align:center">暂无数据</div>';return}
  const W=Math.max(280,el.clientWidth||400),H=180,pl=42,pr=8,pt=12,pb=24;
  const iw=W-pl-pr,ih=H-pt-pb;
  const svg=SE('svg',{width:'100%',height:H,viewBox:`0 0 ${W} ${H}`});
  const maxT=niceMax(Math.max(...s.map(b=>b.in_tok+b.out_tok)));
  for(let i=0;i<=3;i++){
    const y=pt+ih-i*ih/3;
    svg.appendChild(SE('line',{x1:pl,y1:y,x2:W-pr,y2:y,stroke:'#21262d'}));
    const t=SE('text',{x:pl-6,y:y+3,'text-anchor':'end','font-size':10,fill:'#8b949e'});
    t.textContent=fmt(Math.round(maxT*i/3));svg.appendChild(t);
  }
  const step=iw/n,bw=Math.max(2,Math.min(step*0.66,44));
  s.forEach((b,i)=>{
    const cx=pl+i*step+step/2,x=cx-bw/2;
    const hI=b.in_tok/maxT*ih,hO=b.out_tok/maxT*ih;
    if(hI>0.4)svg.appendChild(SE('rect',{x,y:pt+ih-hI,width:bw,height:hI,fill:'#1f6feb',rx:1}));
    if(hO>0.4)svg.appendChild(SE('rect',{x,y:pt+ih-hI-hO,width:bw,height:hO,fill:'#a371f7',rx:1}));
    const hz=SE('rect',{x:pl+i*step,y:pt,width:step,height:ih,fill:'transparent'});
    hz.onmousemove=ev=>chartTip(el,ev,`<b>${new Date(b.t*1000).toLocaleString('zh-CN',{hour12:false})}</b><br>入 ${fmt(b.in_tok)} · 出 ${fmt(b.out_tok)}`);
    hz.onmouseleave=()=>hideTip(el);
    svg.appendChild(hz);
  });
  const tickN=Math.min(6,n);
  for(let i=0;i<tickN;i++){
    const idx=Math.round(i*(n-1)/(tickN-1||1));
    const t=SE('text',{x:pl+idx*step+step/2,y:H-7,'text-anchor':'middle','font-size':10,fill:'#8b949e'});
    t.textContent=bLabel(s[idx].t,d.bucket_s);svg.appendChild(t);
  }
  el.appendChild(svg);
}
function renderModelShare(d){
  const rows=d.by_model||[],tot=rows.reduce((a,m)=>a+m.n,0)||1;
  $('#model-share').innerHTML=rows.slice(0,12).map(m=>{
    const pct=Math.round(100*m.n/tot);
    return `<div class="hrow" onclick="filterByModel('${esc(m.m)}')" title="${esc(m.m)} — ${m.n} 请求 · ${m.errs||0} 错 · 均延 ${Math.round(m.avg_lat)}ms">
      <span class="hname">${esc(m.m)}</span>
      <div class="hbar"><i style="width:${Math.max(1.5,m.n/tot*100)}%"></i></div>
      <span class="hval">${m.n} · ${pct}%${m.errs?` · <span style="color:var(--red)">${m.errs}错</span>`:''}</span></div>`;
  }).join('')||'<div class="muted" style="padding:14px 0">暂无数据</div>';
}
function renderDashAccs(d){
  const pmap={};(d.pool.accounts||[]).forEach(a=>pmap[a.label]=a);
  $('#dash-accs').innerHTML=(d.by_account||[]).map(a=>{
    const p=pmap[a.a]||{};
    const dot=p.state?`<span class="adot ${p.state==='ready'?'ok':p.state==='cooldown'?'err':'off'}" style="margin-right:6px"></span>`:'';
    const rate=a.n?(100*(a.errs||0)/a.n):0;
    return `<tr style="cursor:pointer" onclick="filterByAccount('${esc(a.a)}')">
      <td>${dot}${esc(a.a)}</td><td>${a.n}</td>
      <td>${rate?`<span style="color:${rate>10?'var(--red)':'var(--yellow)'}">${rate.toFixed(1)}%</span>`:'<span class="muted">0%</span>'}</td>
      <td>${fmt((a.in_tok||0)+(a.out_tok||0))}</td>
      <td>${Math.round(a.avg_lat)}ms</td><td class="muted">${fmtT(a.last_used)}</td></tr>`;
  }).join('')||'<tr><td colspan=6 class=muted>窗口内暂无账号流量</td></tr>';
}
function renderDashKeys(d){
  $('#dash-keys').innerHTML=(d.by_key||[]).map(k=>
    `<tr><td>${esc(k.k||'(master/未记录)')}</td><td>${k.n}</td><td>${k.errs?`<span style="color:var(--red)">${k.errs}</span>`:0}</td>
     <td>${fmt(k.tok)}</td><td class="muted">${fmtT(k.last_used)}</td></tr>`).join('')
    ||'<tr><td colspan=5 class=muted>暂无数据</td></tr>';
}
function renderDashErrs(d){
  $('#dash-errs').innerHTML=(d.recent_errors||[]).map(r=>
    `<div class="erow" onclick="showReq(${r.id})">
      <span class="et">${fmtT(r.ts)}</span>
      <span class="tag model">${esc(r.resolved_model||r.model||'-')}</span>
      <span class="emsg" title="${esc(r.error||'')}">[${r.status||'ERR'}] ${esc(r.error||'(无错误信息)')}</span>
      <span class="eacc">${esc(r.account||'')}${r.key_name?' · '+esc(r.key_name):''}</span></div>`).join('')
    ||'<div class="muted" style="padding:16px">窗口内没有错误 ✓</div>';
}
window.addEventListener('resize',(()=>{let t;return()=>{clearTimeout(t);t=setTimeout(()=>{
  if($('#p-dash').classList.contains('on')&&DASH.data){renderReqChart(DASH.data);renderTokChart(DASH.data)}
},180)}})());

// requests
let reqCursor=null, reqHist=[], reqTotal=0, reqAccount='', REQ_LAST_CURSOR=null;   // keyset pagination
function renderAcctChip(){
  $('#rq-acct-chip').innerHTML=reqAccount
    ?`<span class="fchip" title="点击清除账号筛选" onclick="reqAccount='';reqReset();renderAcctChip();loadReqs()">账号: ${esc(reqAccount)} ✕</span>`:'';
}
const PAGE=50;
const FLAG_LABELS={truncated:'断流',retried:'重试',retried_partial:'部分重试',retry_same_account:'同号重试',no_stop_reason:'无stop',client_aborted:'客户端断开',protocol_error:'协议错'};
const flagTags=f=>(f||'').split(',').filter(Boolean).map(x=>` <span class="tag ${x==='truncated'?'err':'off'}">${esc(FLAG_LABELS[x]||x)}</span>`).join('');
const cacheCell=r=>{
  const c=r.cached_tokens||0,tot=c+(r.prompt_tokens||0);
  if(!tot)return '<span class=muted>-</span>';
  const pct=Math.round(100*c/tot);
  return c?`<span style="color:var(--green)" title="命中 ${c}/${tot}">${fmt(c)}<span class="muted"> ${pct}%</span></span>`
        :`<span class=muted title="未命中">${pct}%</span>`};
function reqFilters(){
  const model=$('#rq-model').value, ok=$('#rq-ok').value, q=$('#rq-q').value.trim(), flag=$('#rq-flag').value;
  return (model?`&model=${encodeURIComponent(model)}`:'')+(ok!==''?`&ok=${ok}`:'')+(q?`&q=${encodeURIComponent(q)}`:'')+(flag?`&flag=${encodeURIComponent(flag)}`:'')+(reqAccount?`&account=${encodeURIComponent(reqAccount)}`:'');
}
function reqReset(){reqCursor=null;reqHist=[]}
async function loadReqs(){
  const u=`/admin/api/requests?limit=${PAGE}`+(reqCursor?`&before=${reqCursor}`:'')+reqFilters();
  const d=await (await api(u)).json();
  reqTotal=d.total;
  $('#rq-total').textContent=`共 ${d.total} 条`;
  $('#rq-page').textContent=`第 ${reqHist.length+1} 页`;
  $('#rq-prev').disabled=!reqHist.length;
  $('#rq-next').disabled=!(d.items.length===PAGE&&d.next_cursor);
  $('#rq-body').innerHTML=d.items.map(r=>`<tr${r.ok?'':' class="rerr"'} onclick="showReq(${r.id})">
    <td>${r.id}</td><td>${fmtT(r.ts)}</td>
    <td><span class="tag model">${esc(r.resolved_model||r.model)}</span></td>
    <td class="muted">${esc(r.endpoint||'chat')}</td>
    <td>${r.stream?'✓':''}</td>
    <td><span class="tag ${r.ok?'ok':'err'}">${r.ok?r.status:'ERR'}</span>${flagTags(r.flags)}</td>
    <td>${r.prompt_tokens}</td><td>${r.completion_tokens}</td><td>${cacheCell(r)}</td>
    <td>${r.latency_ms}ms</td><td>${r.ttft_ms??'-'}</td><td>${r.tps??'-'}</td>
    <td>${esc(r.account||'')}</td><td>${esc(r.key_name||'')}</td>
    <td class="ops"><button class="mini" onclick="event.stopPropagation();showReq(${r.id})">详情</button>${r.has_cap?` <button class="mini" onclick="event.stopPropagation();dlCapture(${r.id})" title="导出该请求完整抓包 JSON">📥 抓包</button>`:''}</td></tr>`).join('')||'<tr><td colspan=15 class=muted>暂无请求</td></tr>';
  REQ_LAST_CURSOR=d.next_cursor;
  loadErrRollup().catch(()=>{});   // non-fatal — the table already rendered
}
async function loadErrRollup(){
  const d=await (await api('/admin/api/requests/errors?hours=24')).json();
  const el=$('#rq-errs');
  if(!d.errors||!d.errors.length){el.style.display='none';return}
  el.style.display='block';
  el.innerHTML='<div class="panel" style="padding:10px 14px"><h3 style="margin-bottom:8px">近24h 错误聚合 <span class="muted">点击定位示例请求</span></h3>'
    +d.errors.map(e=>`<div class="erow" onclick="showReq(${e.example_id})">
      <span class="tag err">×${e.n}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.sig)}">${esc(e.sig)}</span>
      <span class="muted" style="flex-shrink:0">${fmtT(e.last)}</span></div>`).join('')+'</div>';
}
$('#rq-id').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.value)showReq(+e.target.value);
});
function reqPage(d){
  if(d>0){const c=REQ_LAST_CURSOR;if(!c)return;reqHist.push(reqCursor);reqCursor=c}
  else{reqCursor=reqHist.pop()??null}
  loadReqs();
}
setInterval(()=>{if($('#p-reqs').classList.contains('on')&&$('#rq-auto').checked)loadReqs().catch(()=>{})},5000);
async function clearReqs(){
  const f=reqFilters();
  const msg=f?`删除当前筛选命中的 ${reqTotal} 条日志？不可恢复。`:'确定清空全部请求日志？不可恢复。';
  if(!confirm(msg))return;
  const d=await (await api('/admin/api/requests/clear?x=1'+f,{method:'POST'})).json();
  reqReset();loadReqs();toast(f?`已删除 ${d.deleted} 条`:'已清空');
}
async function exportReqs(){
  const r=await api('/admin/api/requests/export?fmt=csv&limit=20000'+reqFilters());
  const b=await r.blob();const a=document.createElement('a');
  a.href=URL.createObjectURL(b);
  a.download=(r.headers.get('content-disposition')||'').match(/filename="?([^";]+)/)?.[1]||'requests.csv';
  a.click();URL.revokeObjectURL(a.href);toast('已导出 CSV');
}
async function exportBundle(){
  const r=await api('/admin/api/export');
  const b=await r.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);
  a.download=(r.headers.get('content-disposition')||'').match(/filename="?([^";]+)/)?.[1]||'devin-proxy-diag.zip';
  a.click();URL.revokeObjectURL(a.href);toast('已导出诊断包');
}
let mdRid=null;
async function dlCapture(id){
  id=id||mdRid;if(!id)return;
  const r=await api('/admin/api/requests/'+id+'/capture');
  if(r.status===404){toast('该请求没有抓包数据');return}
  const b=await r.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download=`req-${id}-capture.json`;
  a.click();URL.revokeObjectURL(a.href);toast('已下载抓包');
}

// request detail modal (tabbed)
let MD_TAB='ov';
document.querySelectorAll('#md-tabs a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('#md-tabs a').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('#modal .pane').forEach(x=>x.classList.remove('on'));
  a.classList.add('on');MD_TAB=a.dataset.t;$('#md-p-'+MD_TAB).classList.add('on');
});
const pretty=s=>{if(!s)return'';try{return JSON.stringify(JSON.parse(s),null,1)}catch{return s}};
function renderMsgs(js){
  let arr=[];try{arr=JSON.parse(js||'[]')}catch{}
  if(!Array.isArray(arr)||!arr.length)return '';
  const txt=v=>typeof v==='string'?v:(v==null?'':(Array.isArray(v)?v.map(c=>c?.text||c?.content||JSON.stringify(c)).join(' '):JSON.stringify(v)));
  return arr.map(m=>{
    // chat completions: {role,content[,tool_calls]}
    if(m.role){
      const cls={system:'sys',user:'user',assistant:'asst',tool:'tool'}[m.role]||'tool';
      let body=esc(txt(m.content));
      if(m.tool_calls)body+='\n🔧 '+m.tool_calls.map(t=>esc(t.function?.name||t.name||'')+' '+esc((t.function?.arguments||t.arguments||'').slice(0,120))).join('\n🔧 ');
      return `<div class="msgit ${cls}"><div class="who">${esc(m.role)}</div>${body||'<i class=muted>(空)</i>'}</div>`;
    }
    // responses input items: {type:message|function_call_output|reasoning|item_reference,...}
    const t=m.type||'message';
    if(t==='message')return `<div class="msgit ${m.role==='user'?'user':'asst'}"><div class="who">${esc(m.role||'message')}</div>${esc(txt(m.content))}</div>`;
    if(t==='function_call_output')return `<div class="msgit tool"><div class="who">tool output · ${esc(m.call_id||'')}</div>${esc(txt(m.output)).slice(0,2000)}</div>`;
    if(t==='function_call')return `<div class="msgit tool"><div class="who">tool call · ${esc(m.name||'')}</div>${esc(txt(m.arguments)).slice(0,2000)}</div>`;
    if(t==='reasoning')return `<div class="msgit reason"><div class="who">reasoning</div>${esc(txt(m.summary||m.content)).slice(0,800)}</div>`;
    if(t==='item_reference')return `<div class="msgit tool"><div class="who">ref → ${esc(m.id||'')}</div></div>`;
    return `<div class="msgit tool"><div class="who">${esc(t)}</div>${esc(JSON.stringify(m)).slice(0,800)}</div>`;
  }).join('');
}
const EV_LABEL={request:['请求进入',''],parse_error:['解析失败','err'],mapped:['请求映射',''],
  built:['上游请求',''],attempt:['尝试',''],msg:['上游消息',''],upstream_err:['上游错误','err'],
  exception:['连接异常','err'],proxy_exception:['代理异常','err'],build_error:['构建失败','err'],
  failover:['切换账号','warn'],end:['流结束',''],finish:['完成',''],failed:['最终失败','err'],
  downstream_error:['下游报错','err'],log_cap:['日志截断','warn']};
function evSummary(e){
  const d={...e};delete d.i;delete d.t;delete d.ms;
  if(e.t==='msg'){
    const p=[];
    if(d.mid)p.push('mid='+d.mid);
    if(d.text!=null)p.push('text='+JSON.stringify(d.text).slice(0,220));
    if(d.think!=null)p.push('think='+JSON.stringify(d.think).slice(0,220));
    if(d.tool_calls)p.push('tools='+d.tool_calls.map(t=>`${t.name||'?'}(${(t.arguments||'').length}字)`).join(','));
    if(d.stop)p.push('stop='+d.stop);
    if(d.usage)p.push('usage='+JSON.stringify(d.usage));
    return p.join('  ')||'(空帧)';
  }
  let s=JSON.stringify(d);
  return s.length>600?s.slice(0,600)+'…':s;
}
function renderEvents(js){
  let arr=[];try{arr=JSON.parse(js||'[]')}catch{}
  $('#md-ev').innerHTML=arr.length?arr.map(e=>{
    const[lb,cl]=EV_LABEL[e.t]||[e.t,''];
    return `<div class="evt ${cl}"><span class="ms">+${e.ms}ms</span><span class="kt">${esc(lb)}</span><span class="dt">${esc(evSummary(e))}</span></div>`;
  }).join(''):'<div class="muted" style="padding:10px">无上游事件记录（旧版本请求无此数据）</div>';
}
let MD_REQ=null;
async function showReq(id){
  mdRid=id;
  const r=await (await api('/admin/api/requests/'+id)).json();
  MD_REQ=r;
  $('#md-id').textContent='#'+r.id;
  $('#md-flags').innerHTML=flagTags(r.flags);
  const _cacheTot=(r.cached_tokens||0)+(r.prompt_tokens||0);
  const _cachePct=_cacheTot?Math.round(100*(r.cached_tokens||0)/_cacheTot)+'%':'-';
  $('#md-kv').innerHTML=[['时间',fmtT(r.ts)],['模型',`${esc(r.model)} → ${esc(r.resolved_model)}`],
    ['接口',esc(r.endpoint||'chat')],['状态',r.ok?r.status:'ERR'],['错误',esc(r.error||'-')],
    ['tokens',`${r.prompt_tokens} in / ${r.completion_tokens} out`],
    ['缓存',`${fmt(r.cached_tokens||0)} 读 / ${fmt(r.cache_creation_tokens||0)} 写 · 命中率 ${_cachePct}`],
    ['延迟',r.latency_ms+'ms'],['首token',(r.ttft_ms??'-')+'ms'],['生成耗时',(r.gen_ms??'-')+'ms'],['TPS',r.tps??'-'],
    ['客户端',esc(r.client||'-')],['账号',esc(r.account||'-')],['Key',esc(r.key_name||'-')],
    ['上游',esc([r.upstream_model,r.upstream_msg_id,r.upstream_req_id].filter(Boolean).join(' · ')||'-')]]
    .map(([k,v])=>`<div class=k>${k}</div><div>${v}</div>`).join('');
  $('#md-req').textContent=pretty(r.request_json)||'(未记录请求体)';
  $('#md-msgs').innerHTML=renderMsgs(r.messages_json)||'<span class=muted>(无)</span>';
  renderEvents(r.events_json);
  let sse=[];try{sse=JSON.parse(r.sse_json||'[]')}catch{}
  // non-streaming requests store the response object here, not SSE frames —
  // label + render it accordingly
  $('#md-sse-tab').textContent=r.stream?'SSE 输出':'响应体';
  $('#md-sse').textContent=sse.length
    ?(r.stream?sse.map(s=>'data: '+s).join('\n\n')
               :sse.map(s=>{try{return JSON.stringify(JSON.parse(s),null,1)}catch{return s}}).join('\n\n'))
    :'(无记录)';
  document.querySelector('#md-tabs a').click();   // reset to 概览
  $('#modal').classList.add('on');
}
function copyPane(){
  const el=$('#md-p-'+MD_TAB).querySelector('pre,#md-ev,#md-kv');
  navigator.clipboard.writeText(el?el.textContent:'').then(()=>toast('已复制'));
}
function copyCurl(){
  const r=MD_REQ;if(!r)return;
  let body={};try{body=JSON.parse(r.request_json||'{}')}catch{}
  const ep=(r.endpoint||'chat').includes('response')?'/v1/responses':'/v1/chat/completions';
  const sh=s=>"'"+String(s).replace(/'/g,"'\\''")+"'";
  const cmd=`curl ${location.origin}${ep} \\\n  -H 'Content-Type: application/json' \\\n  -H 'Authorization: Bearer <YOUR_API_KEY>' \\\n  -d ${sh(JSON.stringify(body))}`;
  navigator.clipboard.writeText(cmd).then(()=>toast('curl 命令已复制（key 需自行填入）'));
}

// accounts
let OA_FID=null, ACC_MAP={};
async function loadAccounts(){
  const d=await (await api('/admin/api/accounts')).json();
  const p=d.pool||{};
  ACC_MAP={}; d.accounts.forEach(a=>ACC_MAP[a.id]=a);
  $('#accs-summary').textContent=`共 ${p.total||0} 个 · 就绪 ${p.ready||0} · 冷却 ${p.cooldown||0} · 钉扎会话 ${p.sessions||0}`;
  $('#acc-body').innerHTML=d.accounts.map(a=>{
    const st=a.disabled?'<span class="tag off">禁用</span>'
      :a.state==='cooldown'?`<span class="tag err">冷却 ${a.cooldown_s}s</span>`
      :'<span class="tag ok">就绪</span>';
    const u=a.usage||{};
    return `<tr><td><input type="checkbox" class="acc-cb" data-id="${a.id}" onclick="accSelChanged()"></td><td>${a.id}</td>
      <td><b>${esc(a.label)}</b>${a.email?`<br><span class="muted">${esc(a.email)}</span>`:''}${a.last_error?`<br><span class="muted" style="color:var(--red)" title="${esc(a.last_error)}">${esc(a.last_error.slice(0,60))}</span>`:''}</td>
      <td>${esc(a.plan||'-')}</td><td class="muted">${esc(a.source||'-')}</td>
      <td>${st}</td><td>${u.n||0}</td>
      <td>${a.in_flight}/${a.max_concurrent||'∞'}</td>
      <td class="muted">${a.models&&a.models.length?esc(a.models.join(',')):'全部'}</td>
      <td>${a.fail_count}</td>
      <td>${fmtT(a.last_used)}</td><td class="muted">…${esc(a.token_tail)}</td>
      <td style="white-space:nowrap">
        <button onclick="testAccount(${a.id})">测试</button>
        <button onclick="refreshAccount(${a.id})">刷新</button>
        <button onclick="editAccLimits(${a.id})">限制</button>
        <button onclick="toggleAccount(${a.id},${a.disabled?0:1})">${a.disabled?'启用':'禁用'}</button>
        <button class="danger" onclick="delAccount(${a.id},'${esc(a.label)}')">删除</button>
      </td></tr>`}).join('')||'<tr><td colspan=13 class=muted>暂无账号 — 用上方 OAuth 登录或手动添加</td></tr>';
  const s=await (await api('/admin/api/sessions')).json();
  $('#sess-body').innerHTML=(s.sessions||[]).map(x=>{
    const left=x.expires_in_s;
    const leftTxt=left==null?'-':left<=0?'<span class="tag err">已过期</span>'
      :left<3600?`<span style="color:var(--yellow)">${Math.round(left/60)}分钟</span>`
      :`<span class="muted">${(left/3600).toFixed(1)}小时</span>`;
    return `<tr>
    <td class="muted">${esc(x.session_key)}</td><td>${esc(x.aname||x.email||('#'+x.account_id))}</td>
    <td class="muted">${x.idle_s!=null?fmtDur(x.idle_s)+'前':'-'}</td><td>${leftTxt}</td>
    <td>${fmtT(x.updated)}</td>
    <td><a onclick="unpinSess('${esc(x.session_key)}')">解绑</a></td></tr>`}).join('')
    ||'<tr><td colspan=6 class=muted>暂无钉扎会话</td></tr>';
}
async function oauthStart(){
  $('#oa-err').textContent='';
  const d=await (await api('/admin/api/oauth/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({label:$('#oa-label').value,webapp:$('#oa-webapp').value||null})})).json();
  OA_FID=d.id;
  const u=$('#oa-url');u.href=d.url;u.textContent=d.url;
  $('#oa-box').style.display='block';$('#oa-code').value='';$('#oa-code').focus();
}
async function oauthComplete(){
  if(!OA_FID)return;
  const code=$('#oa-code').value.trim();
  if(!code)return $('#oa-err').textContent='请粘贴 code';
  $('#oa-err').textContent='';
  try{
    const d=await (await api('/admin/api/oauth/'+OA_FID+'/complete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code,label:$('#oa-label').value||null})})).json();
    OA_FID=null;$('#oa-box').style.display='none';
    toast('账号已添加: '+(d.account.label||''));loadAccounts();
  }catch(e){$('#oa-err').textContent=e.message}
}
async function oauthCancel(){
  if(OA_FID)await api('/admin/api/oauth/'+OA_FID,{method:'DELETE'}).catch(()=>{});
  OA_FID=null;$('#oa-box').style.display='none';
}
async function addAccount(){
  const token=$('#acc-token').value.trim();
  if(!token)return toast('请输入 token');
  const d=await (await api('/admin/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:$('#acc-name').value,token})})).json();
  $('#acc-token').value='';$('#acc-name').value='';
  toast('已添加 '+(d.account.label||''));loadAccounts();
}
async function importDetected(){
  const d=await (await api('/admin/api/accounts/import',{method:'POST'})).json();
  toast(`导入 ${d.added} 个`+(d.skipped.length?`，跳过 ${d.skipped.length}`:''));loadAccounts();
}
async function pingAll(){
  toast('检测中…');
  const d=await (await api('/admin/api/ping')).json();
  const bad=d.results.filter(r=>!r.ok);
  toast(d.ok?`✓ 全部 ${d.results.length} 个账号正常（最慢 ${d.latency_ms}ms）`
       :`✗ ${bad.length}/${d.results.length} 个异常: `+bad.map(r=>r.account).join(', '));
  loadAccounts();
}
function accSelectAll(on){document.querySelectorAll('.acc-cb').forEach(cb=>cb.checked=on);accSelChanged()}
function accSelChanged(){
  const sel=[...document.querySelectorAll('.acc-cb:checked')].map(cb=>+cb.dataset.id);
  $('#acc-sel').textContent=sel.length?`已选 ${sel.length} 个`:'';
}
function accSelIds(){return [...document.querySelectorAll('.acc-cb:checked')].map(cb=>+cb.dataset.id)}
async function accBulk(dis){
  const ids=accSelIds();
  if(!ids.length)return toast('先勾选账号');
  const d=await (await api('/admin/api/accounts/bulk',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids,disabled:!!dis})})).json();
  toast(`已${dis?'禁用':'启用'} ${d.updated} 个`);$('#acc-all').checked=false;loadAccounts();
}
async function testAccount(id){
  toast('检测中…');
  const d=await (await api('/admin/api/accounts/'+id+'/test',{method:'POST'})).json();
  toast(d.ok?`✓ 正常 ${d.latency_ms}ms`:`✗ ${d.error}`);loadAccounts();
}
async function refreshAccount(id){
  await api('/admin/api/accounts/'+id+'/refresh',{method:'POST'});toast('已刷新身份信息');loadAccounts();
}
async function toggleAccount(id,dis){
  await api('/admin/api/accounts/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({disabled:!!dis})});
  loadAccounts();
}
async function editAccLimits(id){
  const a=ACC_MAP[id];if(!a)return;
  const mc=prompt('该账号最大并发请求数（0 = 不限）',a.max_concurrent||0);
  if(mc===null)return;
  const models=prompt('该账号可服务的模型（逗号分隔 uid/别名，留空 = 全部）',(a.models||[]).join(','));
  if(models===null)return;
  await api('/admin/api/accounts/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({max_concurrent:parseInt(mc)||0,models})});
  toast('已更新');loadAccounts();
}
async function delAccount(id,name){
  if(!confirm(`删除账号「${name}」？`))return;
  await api('/admin/api/accounts/'+id,{method:'DELETE'});loadAccounts();
}
async function unpinSess(k){
  await api('/admin/api/sessions/'+encodeURIComponent(k),{method:'DELETE'});loadAccounts();
}
async function clearSessions(){
  await api('/admin/api/sessions/clear',{method:'POST'});loadAccounts();
}

// models
const EFF={none:'无思考',low:'低',medium:'中',high:'高',xhigh:'超高',max:'满'};
const EFFORD=['none','low','medium','high','xhigh','max'];
let _famEff={};
function famChip(f,main){
  const tags=[
    main.context?`<span class="tag off">${fmt(main.context)} ctx</span>`:'',
    main.images?'<span class="tag model" title="支持图像输入">👁 图像</span>':'',
    main.thinking?'<span class="tag model" title="支持思考">🧠 思考</span>':'',
    main.credit!=null?`<span class="tag off" title="${esc(main.cost_summary||'credit 消耗倍率')}">×${main.credit}</span>`:'',
    main.effort?`<span class="tag ok" title="默认思考等级：请求未指定 effort 时命中此变体">默认·${esc(EFF[main.effort]||main.effort)}</span>`:'',
    main.remote_accounts?`<span class="tag ok" title="广告此模型的上游账号数">${main.remote_accounts} 账号</span>`:'',
  ].filter(Boolean).join('');
  return `<button class="mchip" onclick="pickModel('${esc(f.prefix)}')" title="${esc(f.prefix)} — 点击在 Playground 试用">
    <b>${esc(f.label)}</b><br><span class="muted" style="font-size:11px">${esc(f.prefix)}</span>
    ${tags?'<br>'+tags:''}</button>`;
}
function modelChip(m,fam,isDef){
  const tags=[
    m.effort?`<span class="tag off">${esc(EFF[m.effort]||m.effort)}</span>`:'',
    m.context?`<span class="tag off">${fmt(m.context)} ctx</span>`:'',
    m.images?'<span class="tag model" title="支持图像输入">👁 图像</span>':'',
    m.thinking?'<span class="tag model" title="支持思考">🧠 思考</span>':'',
    m.alias?`<span class="tag model" title="上游别名">@${esc(m.alias)}</span>`:'',
    m.credit!=null?`<span class="tag off" title="${esc(m.cost_summary||'credit 消耗倍率')}">×${m.credit}</span>`:'',
    m.cost_summary?`<span class="tag off" title="${esc(m.cost_summary)}">💲 ${esc(m.cost_summary.split(' · ')[0].replace(/ \/ .*/,''))}/1M</span>`:'',
    m.remote_accounts?`<span class="tag ok" title="广告此模型的上游账号数">${m.remote_accounts} 账号</span>`:'',
    m.url?'<span class="tag model">URL</span>':'',
    isDef?'<span class="tag ok">★ 默认</span>':'',
  ].filter(Boolean).join('');
  if(m.hidden)
    return `<button class="mchip" style="opacity:.45" onclick="unhideModel('${esc(m.uid)}')"
      title="${esc(m.uid)} — 已隐藏，点击恢复">
      <b style="text-decoration:line-through">${esc(m.label)}</b><br><span class="muted" style="font-size:11px">${esc(m.uid)}</span>
      ${tags?'<br>'+tags:''}</button>`;
  const act=m.effort?`setFamDefault('${esc(fam)}','${esc(m.effort)}')`:`pickModel('${esc(m.uid)}')`;
  const tip=m.effort?'点击设为该家族默认变体':'点击在 Playground 试用';
  return `<button class="mchip" onclick="${act}" title="${esc(m.uid)} — ${tip}">
    <b>${esc(m.label)}</b><br><span class="muted" style="font-size:11px">${esc(m.uid)}
      <span style="cursor:pointer" title="隐藏此变体" onclick="event.stopPropagation();hideModel('${esc(m.uid)}')"> ✕</span></span>
    ${tags?'<br>'+tags:''}</button>`;
}
async function hideModel(uid){
  await api('/admin/api/models/entries/'+encodeURIComponent(uid),{method:'DELETE'});
  toast('已隐藏 '+uid);loadModels().catch(()=>{});
}
async function unhideModel(uid){
  await api('/admin/api/models/entries/unhide',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({uid})});
  toast('已恢复 '+uid);loadModels().catch(()=>{});
}
async function delAlias(a){
  await api('/admin/api/models/aliases/'+encodeURIComponent(a),{method:'DELETE'});
  toast('已删除/隐藏 '+a);loadModels().catch(()=>{});
}
async function unhideAlias(a){
  await api('/admin/api/models/aliases/unhide',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name:a})});
  toast('已恢复 '+a);loadModels().catch(()=>{});
}
async function setFamDefault(fam,eff){
  const d=await (await api('/admin/api/models/settings',{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({family_efforts:{[fam]:eff}})})).json();
  _famEff=d.family_efforts||{};
  toast(eff?`${fam} 默认思考 → ${EFF[eff]||eff}`:`${fam} 默认思考 → 自动`);
  loadModels().catch(()=>{});
}
async function loadModels(){
  const d=await (await api('/admin/api/models')).json();
  const s=d.sync||{};
  $('#model-count').textContent=`${d.models.length} 个`;
  const acctN=Object.keys(s.accounts||{}).length;
  $('#sync-info').innerHTML=[
    s.ts?`最近同步 ${fmtT(s.ts)}`:'尚未同步',
    s.refreshing?'<span class="spin"></span> 同步中':'',
    s.count?`远端 ${s.count} 个`:'',
    acctN?`${acctN} 账号上报`:'',
  ].filter(Boolean).join(' · ')
    +(s.errors&&s.errors.length?` · <span style="color:var(--red)">${s.errors.length} 账号失败：${esc(s.errors.map(e=>`${e.account}: ${e.error}`).join('；').slice(0,200))}</span>`:'')
    +(s.url_err?` · <span style="color:var(--red)">URL 源失败：${esc(s.url_err)}</span>`:'');
  $('#models-url').value=s.url||'';
  const sel=$('#default-model');
  sel.innerHTML='<option value="">自动</option>'
    +d.models.map(m=>`<option${m===d.default_model?' selected':''}>${esc(m)}</option>`).join('')
    +(d.default_model&&!d.models.includes(d.default_model)?`<option selected>${esc(d.default_model)}</option>`:'');
  if(!d.default_model)sel.value='';
  $('#default-effort').value=d.default_effort||'';
  _famEff=d.family_efforts||{};
  $('#alias-edit').value=Object.entries(d.user_aliases||{}).map(([a,t])=>`${a}=${t}`).join('\n');
  $('#model-fams').innerHTML=d.families.map(f=>{
    const prefer=f.default||'medium';
    const vis=f.models.filter(m=>!m.hidden);
    const main=vis.find(m=>m.effort===prefer)||vis.find(m=>m.effort==='medium')||vis[0]||f.models[0];
    const rest=f.models.filter(m=>m!==main);
    const effs=[...new Set(vis.map(m=>m.effort).filter(Boolean))]
      .sort((a,b)=>EFFORD.indexOf(a)-EFFORD.indexOf(b));
    const head=effs.length
      ?`<span style="float:right;font-weight:400;text-transform:none;letter-spacing:0">默认思考
        <select style="padding:2px 6px;font-size:12px" title="请求未指定 effort 时该家族改写到哪个变体（自动 = 跟随全局默认/内置偏好）"
          onchange="setFamDefault('${esc(f.prefix)}',this.value)">
          <option value="">自动${f.default_override?'':(f.default?`（${esc(EFF[f.default]||f.default)}）`:'')}</option>
          ${effs.map(e=>`<option value="${esc(e)}"${e===f.default_override?' selected':''}>${esc(EFF[e]||e)}</option>`).join('')}
        </select> <span class="muted">${f.models.length} 变体</span></span>`
      :`<span class="muted" style="float:right">${f.models.length} 变体</span>`;
    return `<div class="panel"><h3>${esc(f.label)} <span class="tag model">${esc(f.vendor)}</span>
      <span class="muted" style="text-transform:none;letter-spacing:0">${esc(f.desc||'')}</span>
      ${head}</h3>
      <div class="mgrid">${famChip(f,main)}</div>
      ${rest.length?`<details style="margin-top:8px"><summary class="muted" style="cursor:pointer;font-size:12px">展开 ${rest.length} 个变体（点击设为默认）</summary>
        <div class="mgrid" style="margin-top:8px">${rest.map(m=>modelChip(m,f.prefix,false)).join('')}</div></details>`:''}
      </div>`;
  }).join('')
    ||'<div class="panel muted">目录为空 — 点「立即同步」从上游账号拉取（或配置远端目录 URL）</div>';
  const _ua=d.user_aliases||{};
  $('#alias-list').innerHTML=Object.entries(d.aliases||{}).map(([a,t])=>
    `<button style="margin:0 6px 8px 0" onclick="pickModel('${esc(a)}')" title="→ ${esc(t)}">${esc(a)} <span class="muted">→ ${esc(t)}</span>
      <span class="muted" style="margin-left:6px;cursor:pointer" title="${a in _ua?'删除别名':'隐藏别名'}"
        onclick="event.stopPropagation();delAlias('${esc(a)}')">✕</span></button>`).join('')
    ||'<span class="muted">目录为空时别名暂不可用</span>';
  $('#alias-list').innerHTML+=(d.hidden_aliases||[]).map(a=>
    `<button class="muted" style="margin:0 6px 8px 0;text-decoration:line-through;opacity:.6"
      onclick="unhideAlias('${esc(a)}')" title="已隐藏 — 点击恢复">${esc(a)}</button>`).join('');
  $('#model-stats').innerHTML=(d.stats||[]).map(m=>
    `<tr><td><span class="tag model">${esc(m.m)}</span></td><td>${m.n}</td><td>${m.errs||0}</td><td>${fmt(m.in_tok)}</td><td>${fmt(m.out_tok)}</td><td>${fmt(m.cached||0)}</td><td>${(m.cached||m.in_tok)?Math.round(100*(m.cached||0)/((m.cached||0)+(m.in_tok||1)))+'%':'-'}</td><td>${m.avg_tps?m.avg_tps.toFixed(1):'-'}</td><td>${Math.round(m.avg_lat)}ms</td><td>${fmtT(m.last_used)}</td></tr>`).join('')||'<tr><td colspan=10 class=muted>暂无使用记录</td></tr>';
  const rq=$('#rq-model'),cur=rq.value;
  rq.innerHTML='<option value="">全部模型</option>'+d.models.map(m=>`<option>${esc(m)}</option>`).join('');
  rq.value=cur;
}
async function syncModels(){
  $('#sync-btn').disabled=true;
  try{
    const d=await (await api('/admin/api/models/refresh',{method:'POST'})).json();
    toast(d.skipped?('已跳过：'+d.skipped)
      :`同步完成：${d.count} 个模型`+(d.errors&&d.errors.length?`，${d.errors.length} 个账号失败`:'')
      +(d.url_err?'，URL 源失败':''));
  }catch(e){toast('同步失败: '+e.message)}
  $('#sync-btn').disabled=false;loadModels().catch(()=>{});
}
async function saveModelSettings(){
  const d=await (await api('/admin/api/models/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({default_model:$('#default-model').value,
      default_effort:$('#default-effort').value,
      models_url:$('#models-url').value.trim(),aliases:$('#alias-edit').value})})).json();
  toast('已保存 · 默认 '+(d.default_model||'自动'));loadModels().catch(()=>{});
}
function pickModel(m){document.querySelector('[data-p=play]').click();$('#pg-model').value=m;$('#pg-in').focus()}

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

// keys
let KEY_MAP={};
async function loadKeys(){
  const d=await (await api('/admin/api/keys')).json();
  KEY_MAP={}; d.keys.forEach(k=>KEY_MAP[k.id]=k);
  $('#key-body').innerHTML=d.keys.map(k=>`<tr><td>${k.id}</td><td>${esc(k.name)}</td>
    <td>${esc(k.prefix)}…${esc(k.tail)}</td>
    <td class="muted">${esc(k.models||'全部')}</td>
    <td>${k.max_concurrent||'∞'}</td>
    <td>${fmtT(k.created)}</td><td>${fmtT(k.last_used)}</td>
    <td><span class="tag ${k.disabled?'off':'ok'}">${k.disabled?'禁用':'启用'}</span></td>
    <td style="white-space:nowrap"><button onclick="editKey(${k.id})">限制</button>
        <button onclick="toggleKey(${k.id},${k.disabled?0:1})">${k.disabled?'启用':'禁用'}</button>
        <button class="danger" onclick="delKey(${k.id},'${esc(k.name)}')">删除</button></td></tr>`).join('')||'<tr><td colspan=9 class=muted>暂无 key</td></tr>';
}
async function createKey(){
  const name=$('#key-name').value.trim();if(!name)return toast('请输入名称');
  const d=await (await api('/admin/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,models:$('#key-models').value.trim()||null,
      max_concurrent:parseInt($('#key-conc').value)||0})})).json();
  $('#key-name').value='';$('#key-models').value='';$('#key-conc').value='';
  $('#key-reveal').innerHTML=`<div class="reveal">⚠️ 完整 key 只显示这一次，请立即保存：<br><b>${esc(d.key)}</b>
    <a style="margin-left:10px" onclick="navigator.clipboard.writeText('${d.key}');toast('已复制')">复制</a></div>`;
  loadKeys();
}
async function editKey(id){
  const k=KEY_MAP[id];if(!k)return;
  const models=prompt('允许的模型（逗号分隔 uid/别名，留空 = 全部）',k.models||'');
  if(models===null)return;
  const mc=prompt('并发上限（0 = 不限）',k.max_concurrent||0);
  if(mc===null)return;
  await api('/admin/api/keys/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({models,max_concurrent:parseInt(mc)||0})});
  toast('已更新');loadKeys();
}
async function toggleKey(id,dis){await api('/admin/api/keys/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({disabled:!!dis})});loadKeys()}
async function delKey(id,name){
  if(!confirm(`删除 key「${name}」？使用它的客户端会立即失效。`))return;
  await api('/admin/api/keys/'+id,{method:'DELETE'});loadKeys();
}

// status
async function loadConf(){
  const d=await (await api('/admin/api/status')).json();
  const a=d.accounts||{};
  const disk=d.disk||{};
  const diskRow=disk.total
    ?`${fmtB(disk.free)} 可用 / ${fmtB(disk.total)}`
      +(disk.low?' <span style="color:var(--red)">⚠ 空间不足</span>':'')
    :'未知';
  $('#conf-kv').innerHTML=[
    ['账号池',`${a.total||0} 个 · 启用 ${a.enabled||0} · 就绪 ${a.ready||0} · 冷却 ${a.cooldown||0}`],
    ['钉扎会话',a.sessions||0],
    ['API key 保护',d.key_required?'已启用':'未启用（开放）'],
    ['管理后台锁定',d.admin_locked?'是（--api-key）':'否'],
    ['数据库',esc(d.db)],['日志体积',fmtB(d.db_size)],
    ['磁盘剩余空间',diskRow],
    ['已记录请求',fmt(d.requests_logged)+' / 上限 '+fmt(d.max_rows)],
    ['API Keys',d.keys_count],
    ['运行时长',fmtDur(d.uptime_s)],['Python',esc(d.python)]]
    .map(([k,v])=>`<div class=k>${k}</div><div>${v}</div>`).join('');
  $('#foot').textContent=`账号 ${a.ready||0}/${a.total||0} 就绪`;
}
async function freeSpace(){
  $('#free-space-btn').disabled=true;
  $('#free-space-out').innerHTML='<span class="spin"></span> 清理中…';
  try{
    const d=await (await api('/admin/api/maintenance/cleanup?aggressive=true',
      {method:'POST'})).json();
    $('#free-space-out').textContent=
      `已释放 ${fmtB(d.freed_bytes||0)}（DB ${fmtB(d.db_size_before)} → ${fmtB(d.db_size_after)}）`;
    toast('清理完成');
    loadConf();
  }catch(e){$('#free-space-out').textContent='清理失败: '+e.message}
  $('#free-space-btn').disabled=false;
}
async function pingUp(){
  $('#ping-btn').disabled=true;$('#ping-out').innerHTML='<span class="spin"></span> 检测中…';
  try{
    const d=await (await api('/admin/api/ping')).json();
    $('#ping-out').innerHTML=(d.results||[]).map(r=>
      r.ok?`<span style="color:var(--green)">✓ ${esc(r.account)} ${r.latency_ms}ms</span>`
        :`<span style="color:var(--red)">✗ ${esc(r.account)} ${esc(r.error||'')}</span>`
    ).join(' &nbsp;·&nbsp; ')||'<span class="muted">无启用账号</span>';
  }catch(e){$('#ping-out').textContent='检测失败: '+e.message}
  $('#ping-btn').disabled=false;
}

async function boot(){
  try{ await Promise.all([loadDash(),loadConf()]) }catch(e){}
}
setInterval(()=>{if($('#p-dash').classList.contains('on')&&$('#dash-auto').checked)loadDash().catch(()=>{})},15000);
boot();
