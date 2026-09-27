// models
const EFF={none:'无思考',low:'低',medium:'中',high:'高',xhigh:'超高',max:'满'};
const EFFORD=['none','low','medium','high','xhigh','max'];
let _famEff={};

// ---- helpers -------------------------------------------------------------
function _price(e){
  // cost_summary = "$4 / 1M Input · $0.2 / 1M Cached input · $20 / 1M Output"
  const cs=e.cost_summary||'';
  const inP=(cs.match(/\$([\d.]+)\s*\/\s*1M Input/i)||[])[1];
  const outP=(cs.match(/\$([\d.]+)\s*\/\s*1M Output/i)||[])[1];
  if(inP!=null||outP!=null)return {i:inP,o:outP,raw:cs};
  return e.credit!=null?{i:null,o:null,raw:`×${e.credit}`}:{i:null,o:null,raw:'—'};
}
function _traits(e){
  // collect capability names from upstream traits + flags
  const t=new Set();
  (e.traits||[]).forEach(x=>{if(x.name)t.add(x.name)});
  if(e.thinking)t.add('Thinking');
  if(e.images)t.add('Vision');
  return t;
}
function _badges(e){
  const t=_traits(e),out=[];
  if(e.promo)out.push('<span class="tag ok" title="优惠/促销模型 — 上游折扣价">促销</span>');
  if(/fast/i.test(e.uid)||t.has('Fast Mode'))out.push('<span class="tag model" title="Fast mode 变体">⚡ Fast</span>');
  if(/-1m\b|1m/i.test(e.uid)||t.has('1M Context'))out.push('<span class="tag model" title="1M context 变体">1M</span>');
  if(t.has('Thinking'))out.push('<span class="tag model" title="支持思考">🧠</span>');
  if(t.has('Vision'))out.push('<span class="tag model" title="支持图像输入">👁</span>');
  return out.join('');
}
function _rl(e){
  const r=e.rate_limit;return r&&r.cap?`<span class="tag off" title="限额窗口 · cap ${fmt(r.cap)}${r.reset_ts?' · 重置 '+fmtT(r.reset_ts):''}">⏳</span>`:'';
}

// ---- per-variant table row -------------------------------------------------
function _row(m,fam,isDef){
  const p=_price(m),h=m.hidden?' style="opacity:.45;text-decoration:line-through"':'';
  const act=m.effort?`setFamDefault('${esc(fam)}','${esc(m.effort)}')`:`pickModel('${esc(m.uid)}')`;
  const op=m.hidden
    ?`<a href="javascript:void 0" onclick="unhideModel('${esc(m.uid)}')">恢复</a>`
    :`<a href="javascript:void 0" title="${m.effort?'设为家族默认变体':'在 Playground 试用'}" onclick="${act}">${m.effort?'设默认':'试用'}</a>
       <a href="javascript:void 0" class="muted" title="隐藏此变体" onclick="hideModel('${esc(m.uid)}')"> 隐藏</a>`;
  return `<tr${h}>
    <td><code>${esc(m.uid)}</code>${m.alias?` <span class="tag off" title="上游别名">@${esc(m.alias)}</span>`:''}</td>
    <td>${esc(m.label)}${m.effort?` <span class="tag off">${esc(EFF[m.effort]||m.effort)}</span>`:''}
      ${isDef?'<span class="tag ok">★ 默认</span>':''}</td>
    <td class="num">${m.context?fmt(m.context):'—'}</td>
    <td class="num">${m.max_output?fmt(m.max_output):'—'}</td>
    <td class="num" title="${esc(p.raw)}">${p.i!=null?`$${p.i}`:(p.raw&&p.raw.startsWith('×')?p.raw:'—')}</td>
    <td class="num" title="${esc(p.raw)}">${p.o!=null?`$${p.o}`:'—'}</td>
    <td>${_badges(m)} ${_rl(m)}${m.remote_accounts?`<span class="tag ok" title="广告此模型的上游账号数">${m.remote_accounts}账号</span>`:''}${m.url?'<span class="tag model">URL</span>':''}</td>
    <td style="white-space:nowrap">${op}</td></tr>`;
}

// ---- family panel: header + variant table ----------------------------------
function _famPanel(f){
  const prefer=f.default||'medium';
  const vis=f.models.filter(m=>!m.hidden);
  const main=vis.find(m=>m.effort===prefer)||vis.find(m=>m.effort==='medium')||vis[0]||f.models[0];
  const effs=[...new Set(vis.map(m=>m.effort).filter(Boolean))]
    .sort((a,b)=>EFFORD.indexOf(a)-EFFORD.indexOf(b));
  const promo=f.models.some(m=>m.promo);
  const maxCtx=Math.max(0,...f.models.map(m=>m.context||0));
  const head=effs.length
    ?`<span style="float:right;font-weight:400;text-transform:none;letter-spacing:0">默认思考
      <select style="padding:2px 6px;font-size:12px" title="请求未指定 effort 时该家族改写到哪个变体（自动 = 跟随全局默认/内置偏好）"
        onchange="setFamDefault('${esc(f.prefix)}',this.value)">
        <option value="">自动${f.default_override?'':(f.default?`（${esc(EFF[f.default]||f.default)}）`:'')}</option>
        ${effs.map(e=>`<option value="${esc(e)}"${e===f.default_override?' selected':''}>${esc(EFF[e]||e)}</option>`).join('')}
      </select> <span class="muted">${f.models.length} 变体</span></span>`
    :`<span class="muted" style="float:right">${f.models.length} 变体</span>`;
  const sorted=[...f.models].sort((a,b)=>
    EFFORD.indexOf(a.effort||'none')-EFFORD.indexOf(b.effort||'none')||a.uid.localeCompare(b.uid));
  return `<div class="panel">
    <h3>${esc(f.label)} <span class="tag model">${esc(f.vendor)}</span>
      ${promo?'<span class="tag ok" title="该家族含促销/折扣模型">促销</span>':''}
      ${maxCtx?`<span class="tag off" title="最大上下文">${fmt(maxCtx)} ctx</span>`:''}
      <span class="muted" style="text-transform:none;letter-spacing:0">${esc(f.desc||'')}</span>
      ${head}</h3>
    <table>
      <thead><tr><th style="width:24%">模型 ID</th><th style="width:22%">显示名</th>
        <th class="num">上下文</th><th class="num">输出</th>
        <th class="num" title="每 1M input tokens">输入</th><th class="num" title="每 1M output tokens">输出价</th>
        <th style="width:22%">能力</th><th>操作</th></tr></thead>
      <tbody>${sorted.map(m=>_row(m,f.prefix,m===main)).join('')}</tbody></table>
    <div class="muted" style="margin-top:6px;font-size:12px">
      <code style="cursor:pointer" title="点击在 Playground 试用该家族"
        onclick="pickModel('${esc(f.prefix)}')">${esc(f.prefix)}</code>
      别名请求 → ${esc(main?main.uid:'')}</div>
  </div>`;
}

// ---- actions ---------------------------------------------------------------
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
  $('#model-fams').innerHTML=d.families.map(_famPanel).join('')
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
  $('#model-stats').innerHTML=(d.stats||[]).map(m=>{
    const tot=(m.in_tok||0);
    const hitPct=tot?Math.round(100*(m.cached||0)/tot)+'%':'-';
    return `<tr><td><span class="tag model">${esc(m.m)}</span></td><td>${m.n}</td><td>${m.errs||0}</td><td title="含缓存读 ${fmt(m.cached||0)}">${fmt(tot)}</td><td>${fmt(m.out_tok)}</td><td>${fmt(m.cached||0)}</td><td>${hitPct}</td><td>${m.avg_tps?m.avg_tps.toFixed(1):'-'}</td><td>${Math.round(m.avg_lat)}ms</td><td>${fmtT(m.last_used)}</td></tr>`}).join('')||'<tr><td colspan=10 class=muted>暂无使用记录</td></tr>';
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
