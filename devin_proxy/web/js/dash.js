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

// ---------- live feed (WebSocket push; replaces poll for pool state) ----------
let LIVE={ws:null,connecting:false,retry:0,authRetry:false,dv:null,refreshT:null};
async function connectLive(){
  if(LIVE.ws&&(LIVE.ws.readyState===0||LIVE.ws.readyState===1))return;
  if(LIVE.connecting)return;
  LIVE.connecting=true;
  // ticket auth first — immune to proxies that rewrite Host/forwarded
  // headers; cookie auth on the bare URL stays as the fallback.
  let url=(location.protocol==='https:'?'wss://':'ws://')
    +location.host+'/admin/api/ws';
  try{
    const t=await (await api('/admin/api/ws-ticket')).json();
    url+='?t='+encodeURIComponent(t.ticket);
  }catch(e){}
  LIVE.connecting=false;
  const ws=new WebSocket(url);
  LIVE.ws=ws;
  ws.onopen=()=>{LIVE.retry=0;LIVE.authRetry=false;liveDot(true)};
  ws.onmessage=ev=>{
    let m;try{m=JSON.parse(ev.data)}catch(e){return}
    if(m.type!=='live')return;
    applyLive(m);
    if(LIVE.dv!==null&&m.data_v!==LIVE.dv)scheduleRefresh();
    LIVE.dv=m.data_v;
  };
  ws.onclose=ev=>{
    liveDot(false);
    if(ev.code!==1000&&ev.code!==1005)
      console.warn('live ws closed:',ev.code,ev.reason||'');
    // 4401 = the handshake cookie expired mid-session. The polling calls
    // may have already slid-renewed the jar, so retry once before
    // concluding we're really logged out.
    if(ev.code===4401){
      if(LIVE.authRetry){location.href='/admin';return}
      LIVE.authRetry=true;setTimeout(connectLive,400);return;
    }
    LIVE.retry=Math.min(LIVE.retry+1,5);
    setTimeout(connectLive,1500*LIVE.retry);
  };
}
function liveDot(on){
  const d=document.querySelector('.livedot');
  if(d){d.classList.toggle('off',!on);d.title=on?'实时推送已连接':'实时推送已断开，重连中…'}
}
function scheduleRefresh(){
  if(!$('#dash-auto').checked)return;
  clearTimeout(LIVE.refreshT);
  LIVE.refreshT=setTimeout(()=>loadDash().catch(()=>{}),1200);
}
function applyLive(m){
  const p=m.pool||{};
  if(DASH.data){
    DASH.data.pool=p;DASH.data.uptime_s=m.uptime_s;
    renderDashCards(DASH.data);renderPool(DASH.data);
    $('#dash-time').textContent='实时 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }
  $('#foot').textContent=`账号 ${p.ready||0}/${p.total||0} 就绪 · 在途 ${p.in_flight||0}`;
  applyLiveAccs(p);
}
// accs page: patch 状态/在途 cells in place; if the pushed row count no
// longer matches what's rendered an add/remove happened — the next full
// loadAccounts() fixes it, so just skip.
function applyLiveAccs(p){
  if(!$('#p-accs').classList.contains('on'))return;
  $('#accs-summary').textContent=`共 ${p.total||0} 个 · 就绪 ${p.ready||0} · 冷却 ${p.cooldown||0} · 钉扎会话 ${p.sessions||0}`;
  const accs=p.accounts||[],rows=$('#acc-body').querySelectorAll('tr');
  if(accs.length!==rows.length)return;
  accs.forEach((a,i)=>{
    const tds=rows[i].querySelectorAll('td');
    if(tds.length<8)return;
    tds[5].innerHTML=a.state==='disabled'?'<span class="tag off">禁用</span>'
      :a.state==='cooldown'?`<span class="tag err">冷却 ${a.cooldown_s}s</span>`
      :'<span class="tag ok">就绪</span>';
    tds[7].textContent=`${a.in_flight}/${a.max_concurrent||'∞'}`;
  });
}
