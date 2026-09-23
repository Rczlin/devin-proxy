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
