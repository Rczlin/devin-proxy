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
