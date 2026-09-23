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

// current page's loader — manual refresh buttons call this
async function refreshPage(btn){
  const p=document.querySelector('.nav a.on');
  const fn=p&&({dash:loadDash,reqs:loadReqs,accs:loadAccounts,models:loadModels,play:loadPlayground,keys:loadKeys,conf:loadConf})[p.dataset.p];
  if(!fn)return;
  if(btn){btn.disabled=true;btn.classList.add('spinning')}
  try{await fn()}catch(e){toast('刷新失败: '+e.message)}
  if(btn){btn.disabled=false;btn.classList.remove('spinning')}
}

// auto-refresh loop: ticks while the page is visible + checkbox on,
// interval comes from a per-page <select> persisted in localStorage.
// Awaits each load before scheduling the next tick — no overlap.
function autoRefresh(checkId,intvId,pageSel,fn){
  const cb=$('#'+checkId),sel=$('#'+intvId);
  if(!cb)return;
  if(sel){
    const saved=localStorage.getItem('intv:'+intvId);
    if(saved&&[...sel.options].some(o=>o.value===saved))sel.value=saved;
    sel.onchange=()=>localStorage.setItem('intv:'+intvId,sel.value);
    sel.disabled=!cb.checked;
    cb.addEventListener('change',()=>sel.disabled=!cb.checked);
  }
  const tick=async()=>{
    if(cb.checked&&$(pageSel).classList.contains('on'))await fn().catch(()=>{});
    const s=sel?(+sel.value||15):15;
    setTimeout(tick,Math.max(2,s)*1000);
  };
  setTimeout(tick,sel?(+sel.value||15)*1000:15000);
}

// nav
document.querySelectorAll('.nav a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('on'));
  a.classList.add('on'); $('#p-'+a.dataset.p).classList.add('on');
  const fn=({dash:loadDash,reqs:loadReqs,accs:loadAccounts,models:loadModels,play:loadPlayground,keys:loadKeys,conf:loadConf})[a.dataset.p];
  fn().catch(e=>toast('加载失败: '+e.message));   // one bad page must not break nav
});
