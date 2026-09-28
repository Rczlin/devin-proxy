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

function openInflight(){
  $('#if-modal').classList.add('on');
  IF.sel=null;
  renderInflight();
  // tick every 500ms so elapsed/stall update between ws pushes
  clearInterval(IF.tickId);
  IF.tickId=setInterval(()=>{IF.dirty=true;scheduleRender()},500);
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
function ifRow(r){
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
  return `<div class="ifrow${IF.sel===r.id?' on':''}${r.phase==='done'?' done':''}" onclick="ifSel(${r.id})">
    <div class="ifmain">
      <span class="tag ${cl}">${lb}</span>
      <span class="ifsub">${sub}</span>
      ${r.tool_calls?`<span class="tag model">🔧${r.tool_calls}</span>`:''}
      ${r.attempt>1?`<span class="tag warn">试${r.attempt}</span>`:''}
    </div>
    <div class="ifstats">${stats}<span class="muted">#${r.id} · ${r.chunks}帧</span></div>
  </div>`;
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
  $('#if-list').innerHTML=items.length
    ? items.map(ifRow).join('')
    : '<div class="muted" style="padding:18px;text-align:center">当前没有在途请求</div>';
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
  $('#if-detail').innerHTML=
    `<div class="ifdetail"><div class="ifkv">${kv}</div>`+
    (r.tail?`<div class="iftail-wrap"><div class="muted" style="margin-bottom:4px">输出预览（末尾 ${r.tail.length} 字符）</div><pre class="iftail">${esc(r.tail)}</pre></div>`:'')+
    `</div>`;
}
// hooked by applyLive() in dash.js — refreshes the modal when open
function applyLiveInflight(f){
  IF.items=f.inflight||[];IF.lastPush=Date.now();IF.dirty=true;
  scheduleRender();
}
