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
  const _cacheTot=(r.prompt_tokens||0)+(r.cached_tokens||0);   // total input
  const _cachePct=_cacheTot?Math.round(100*(r.cached_tokens||0)/_cacheTot)+'%':'-';
  $('#md-kv').innerHTML=[['时间',fmtT(r.ts)],['模型',`${esc(r.model)} → ${esc(r.resolved_model)}`],
    ['接口',esc(r.endpoint||'chat')],['状态',r.ok?r.status:'ERR'],['错误',esc(r.error||'-')],
    ['输入',`${fmt(r.prompt_tokens||0)} 新读 + ${fmt(r.cached_tokens||0)} 缓存读 = ${fmt(_cacheTot)} 总计`],
    ['输出',`${fmt(r.completion_tokens||0)}`],
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
