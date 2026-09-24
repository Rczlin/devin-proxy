// requests
let reqCursor=null, reqHist=[], reqTotal=0, reqAccount='', REQ_LAST_CURSOR=null;   // keyset pagination
function renderAcctChip(){
  $('#rq-acct-chip').innerHTML=reqAccount
    ?`<span class="fchip" title="点击清除账号筛选" onclick="reqAccount='';reqReset();renderAcctChip();loadReqs()">账号: ${esc(reqAccount)} ✕</span>`:'';
}
const PAGE=50;
const FLAG_LABELS={truncated:'断流',retried:'重试',retried_partial:'部分重试',retry_same_account:'同号重试',no_stop_reason:'无stop',client_aborted:'客户端断开',protocol_error:'协议错'};
const flagTags=f=>(f||'').split(',').filter(Boolean).map(x=>` <span class="tag ${x==='truncated'?'err':'off'}">${esc(FLAG_LABELS[x]||x)}</span>`).join('');
const inTok=r=>(r.prompt_tokens||0)+(r.cached_tokens||0);
const inTokCell=r=>{
  const tot=inTok(r),c=r.cached_tokens||0;
  return c?`<span title="总输入 ${fmt(tot)} = 未缓存 ${fmt(r.prompt_tokens||0)} + 缓存读 ${fmt(c)}">${fmt(tot)}<span class="muted"> (${fmt(c)}↩)</span></span>`
        :`${fmt(tot)}`};
const cacheCell=r=>{
  const c=r.cached_tokens||0,tot=inTok(r);
  if(!tot)return '<span class=muted>-</span>';
  const pct=Math.round(100*c/tot);
  return c?`<span style="color:var(--green)" title="缓存读 ${fmt(c)} / 总输入 ${fmt(tot)}">${fmt(c)}<span class="muted"> ${pct}%</span></span>`
        :`<span class=muted title="无缓存读">${pct}%</span>`};
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
    <td>${inTokCell(r)}</td><td>${r.completion_tokens}</td><td>${cacheCell(r)}</td>
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
autoRefresh('rq-auto','rq-intv','#p-reqs',loadReqs);
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
