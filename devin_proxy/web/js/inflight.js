// ---------- in-flight requests live modal ----------
// Driven by the ws live frame's `inflight` array (see admin_routes/live.py).
// Falls back to REST snapshot when the modal opens while ws is down.
let IF={items:[],sel:null,lastPush:0,dirty:false,rafId:0,tickId:0};

const IF_PHASE={
  connecting:['等待上游','wait'],
  streaming:['流式输出中','ok'],
  stalled:['停顿中','warn'],
  done:['已完成','off'],
};
const ifAge=s=>s==null?'-':s<60?s.toFixed(1)+'s':s<3600?Math.floor(s/60)+'m'+(s%60).toFixed(1)+'s':(s/3600).toFixed(1)+'h';

// one delegated click handler on the list — survives DOM updates and
// doesn't depend on inline onclick surviving a re-render.
let IF_DELEGATE=false;
function bindInflightOnce(){
  if(IF_DELEGATE)return;
  IF_DELEGATE=true;
  $('#if-list').addEventListener('click',e=>{
    const row=e.target.closest('.ifrow');
    if(row)ifSel(+row.dataset.id);
  });
}
function openInflight(){
  bindInflightOnce();
  $('#if-modal').classList.add('on');
  ifSubscribe();              // ask the ws feed to include inflight frames
  IF.sel=null;
  renderInflight();
  // tick every 100ms so elapsed/stall update between ws pushes —
  // matches the 0.1s display precision of ifAge()
  clearInterval(IF.tickId);
  IF.tickId=setInterval(()=>{IF.dirty=true;scheduleRender()},100);
  // REST fallback — if the ws feed is dead we still show a snapshot,
  // otherwise the next live frame repaints over it instantly.
  api('/admin/api/inflight').then(r=>r.json()).then(d=>{
    if(!IF.items.length){IF.items=d.items||[];IF.lastPush=Date.now();IF.dirty=true;scheduleRender()}
  }).catch(()=>{});
}
const IF_EV_LABEL={
  text:'文本',think:'思考',tool_call:'工具调用',
  'stop:0':'正常结束','stop:1':'长度截断','stop:2':'内容过滤',
  'stop:3':'异常终止','stop:4':'上游断开',
};
const ifEvLabel=s=>{
  if(!s)return '-';
  if(IF_EV_LABEL[s])return IF_EV_LABEL[s];
  if(s.startsWith('stop:'))return 'stop:'+s.slice(5);
  if(s.startsWith('err:'))return '⚠ '+s.slice(4);
  return s;
};

function ifElapsed(r){
  // done: use the frozen elapsed from the server
  if(r.phase==='done')return r.elapsed_s;
  // live: anchor to last server-computed elapsed_s, add client-side
  // delta since that push — avoids Date.now() vs server-clock skew
  const pushAge=(Date.now()-IF.lastPush)/1000;
  return r.elapsed_s+pushAge;
}
// Build/update one row's inner content (everything except the row root's
// own data-id / click wiring, which are stable per id). Reused by both the
// keyed update path and the initial build.
function ifRowHTML(r){
  const [lb,cl]=IF_PHASE[r.phase]||[r.phase,''];
  const tok=r.out_tokens==null?'-':fmt(r.out_tokens)+(r.out_tokens_est?'~':'');
  const sub=[
    r.requested_model&&r.requested_model!==r.model?esc(r.requested_model)+' → ':'',
    `<b>${esc(r.model||'-')}</b>`, ' · ', esc(r.endpoint||'-'),
    r.stream?' · SSE':'', ' · ', esc(r.account||'-'),
    r.key_name?' · key:'+esc(r.key_name):'', r.client?' · '+esc(r.client):'',
  ].join('');
  const stats=[
    ['耗时',ifAge(ifElapsed(r))],
    ['TTFT',r.ttft_ms==null?'-':r.ttft_ms+'ms'],
    ['输出',tok+' tok'],
    ['TPS',r.tps==null?'-':r.tps],
    ['停顿',r.stall_s==null?'-':r.stall_s+'s'],
  ].map(([k,v])=>`<span class="ifst"><i>${k}</i>${v}</span>`).join('');
  return `<div class="ifmain">
      <span class="tag ${cl}">${lb}</span>
      <span class="ifsub">${sub}</span>
      ${r.tool_calls?`<span class="tag model">🔧${r.tool_calls}</span>`:''}
      ${r.attempt>1?`<span class="tag warn">试${r.attempt}</span>`:''}
    </div>
    <div class="ifstats">${stats}<span class="muted">#${r.id} · ${r.chunks}帧</span></div>`;
}
// apply one item's state to an existing row node: refresh inner HTML only
// when it actually changed, then sync the state classes on the root.
function patchIfRow(node,r){
  const html=ifRowHTML(r);
  if(node._h!==html){node.innerHTML=html;node._h=html}
  node.classList.toggle('on',IF.sel===r.id);
  node.classList.toggle('done',r.phase==='done');
}
function renderInflight(){
  const items=IF.items;
  const live=items.filter(r=>r.phase!=='done');
  const done=items.filter(r=>r.phase==='done');
  $('#if-sub').textContent=live.length
    ?`共 ${live.length} 个进行中`+(done.length?` · ${done.length} 个最近完成`:'')
    :done.length?`${done.length} 个最近完成`:'';
  const scroller=$('#if-modal .box');
  // auto-scroll: if the user is near the bottom, keep them pinned there
  // as new rows arrive; if they scrolled up, leave them alone.
  const wasNearBottom=scroller.scrollTop+scroller.clientHeight>=scroller.scrollHeight-50;
  const list=$('#if-list');
  if(!items.length){
    if(!list._empty){list.innerHTML='<div class="muted" style="padding:18px;text-align:center">当前没有在途请求</div>';list._empty=true;list._rows={}}
  }else{
    list._empty=false;
    list._rows=list._rows||{};
    const seen=new Set();
    // Walk items in order; for each, ensure its node sits at DOM index i.
    // Existing nodes are moved (not recreated), new ids get a fresh node —
    // so a live row keeps its DOM element, its click target, and any
    // in-progress text selection while its numbers update.
    items.forEach((r,i)=>{
      seen.add(r.id);
      let node=list._rows[r.id];
      if(!node){
        node=document.createElement('div');
        node.className='ifrow';node.dataset.id=r.id;node._h=null;
        list._rows[r.id]=node;
      }
      patchIfRow(node,r);
      // node should be the i-th child; insertBefore moves it there (a
      // no-op move when it's already correctly positioned is avoided).
      if(list.children[i]!==node)list.insertBefore(node,list.children[i]||null);
    });
    // drop rows that disappeared from the feed
    for(const id in list._rows){
      if(!seen.has(+id)){list._rows[id].remove();delete list._rows[id]}
    }
  }
  if(wasNearBottom)scroller.scrollTop=scroller.scrollHeight;
  if(IF.sel!=null){
    const r=items.find(x=>x.id===IF.sel);
    if(r)renderIfDetail(r);
    else{IF.sel=null;$('#if-detail').style.display='none'}
  }
}
// schedule a render via rAF — collapses rapid ws pushes into one paint
function scheduleRender(){
  if(IF.rafId)return;
  IF.rafId=requestAnimationFrame(()=>{
    IF.rafId=0;
    const open=$('#if-modal').classList.contains('on');
    if(!open){clearInterval(IF.tickId);IF.tickId=0;IF.dirty=false;return}
    renderInflight();
    if(IF.dirty){IF.dirty=false;scheduleRender()}
  });
}
function ifSel(id){
  IF.sel=IF.sel===id?null:id;
  $('#if-detail').style.display=IF.sel==null?'none':'block';
  scheduleRender();
}
function renderIfDetail(r){
  const tok=r.out_tokens==null?'-':fmt(r.out_tokens)+(r.out_tokens_est?'（估算）':'');
  const kv=[['状态',(IF_PHASE[r.phase]||[r.phase])[0]],
    ['模型',`${esc(r.requested_model||'-')} → ${esc(r.model||'-')}`],
    ['接口',esc(r.endpoint||'-')+(r.stream?' · stream':'')],
    ['账号',esc(r.account||'-')],['API Key',esc(r.key_name||'-')],
    ['客户端',esc(r.client||'-')],['会话',esc(r.session_key||'-')],
    ['开始',new Date(r.t0*1000).toLocaleTimeString('zh-CN',{hour12:false})],
    ['已耗时',ifAge(r.elapsed_s)],['首帧耗时',r.ttft_ms==null?'-':r.ttft_ms+'ms'],
    ['输出 tokens',tok],['输入 tokens',r.in_tokens==null?'-':fmt(r.in_tokens)],
    ['正文/思考字符',`${fmt(r.text_chars)} / ${fmt(r.think_chars)}`],
    ['上游帧数',r.chunks],['工具调用',r.tool_calls],['尝试次数',r.attempt],
    ['实时 TPS',r.tps==null?'-':r.tps+' tok/s'],
    ['距上帧',r.stall_s==null?'-':r.stall_s+'s'],['最近事件',esc(ifEvLabel(r.last_ev))]]
    .map(([k,v])=>`<div class=k>${k}</div><div>${v}</div>`).join('');
  const el=$('#if-detail');
  const html=
    `<div class="ifdetail"><div class="ifkv">${kv}</div>`+
    (r.tail?`<div class="iftail-wrap"><div class="muted" style="margin-bottom:4px">输出预览（末尾 ${r.tail.length} 字符）</div><pre class="iftail">${esc(r.tail)}</pre></div>`:'')+
    `</div>`;
  // keep the expanded detail stable: only rewrite when content changed,
  // so text doesn't flicker / lose selection every refresh tick.
  const tail=el.querySelector('.iftail');
  const tailAtBottom=tail&&(tail.scrollTop+tail.clientHeight>=tail.scrollHeight-8);
  if(el._h!==html){
    el.innerHTML=html;el._h=html;
    const nt=el.querySelector('.iftail');
    if(nt&&tailAtBottom)nt.scrollTop=nt.scrollHeight;
  }
}
// hooked by applyLive() in dash.js — refreshes the modal when open
function applyLiveInflight(f){
  if(f.inflight==null)return;   // unsubscribed frames carry no inflight
  IF.items=f.inflight||[];IF.lastPush=Date.now();IF.dirty=true;
  scheduleRender();
}

// ---------- inflight subscription over the live ws ----------
// Server pushes the inflight array only while at least one subscriber
// asks for it — the modal sends sub/unsub so the feed can skip the
// per-second snapshot when nobody is looking.
let IF_OBS=null;
function ifSubscribe(){
  const ws=LIVE.ws;
  if(ws&&ws.readyState===1)ws.send('{"sub":"inflight"}');
}
function ifUnsubscribe(){
  const ws=LIVE.ws;
  if(ws&&ws.readyState===1)ws.send('{"unsub":"inflight"}');
}
function bindInflightSub(){
  if(IF_OBS)return;
  const modal=$('#if-modal');
  if(!modal)return;
  IF_OBS=new MutationObserver(()=>{
    if(!modal.classList.contains('on'))ifUnsubscribe();
  });
  IF_OBS.observe(modal,{attributes:true,attributeFilter:['class']});
}
bindInflightSub();
