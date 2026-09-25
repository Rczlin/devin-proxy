// status
async function loadConf(){
  const d=await (await api('/admin/api/status')).json();
  const a=d.accounts||{};
  const disk=d.disk||{};
  const per=disk.per_db||{};
  const diskRow=disk.total
    ?`${fmtB(disk.free)} 可用 / ${fmtB(disk.total)}`
      +(disk.low?' <span style="color:var(--red)">⚠ 空间不足</span>':'')
    :'未知';
  const dbRow=[['配置',per.core],['请求日志',per.log],['诊断',per.diag]]
    .filter(([,s])=>s!=null).map(([k,s])=>`${k} ${fmtB(s)}`).join(' · ');
  $('#conf-kv').innerHTML=[
    ['账号池',`${a.total||0} 个 · 启用 ${a.enabled||0} · 就绪 ${a.ready||0} · 冷却 ${a.cooldown||0}`],
    ['钉扎会话',a.sessions||0],
    ['API key 保护',d.key_required?'已启用':'未启用（开放）'],
    ['管理后台锁定',d.admin_locked?'是（--api-key）':'否'],
    ['数据库',esc(d.db)],
    ['存储分布',dbRow||fmtB(d.db_size)],
    ['磁盘剩余空间',diskRow],
    ['已记录请求',fmt(d.requests_logged)+' / 上限 '+fmt(d.max_rows)],
    ['API Keys',d.keys_count],
    ['运行时长',fmtDur(d.uptime_s)],['Python',esc(d.python)]]
    .map(([k,v])=>`<div class=k>${k}</div><div>${v}</div>`).join('');
  $('#foot').textContent=`账号 ${a.ready||0}/${a.total||0} 就绪`;
}
async function freeSpace(nuke){
  $('#free-space-btn').disabled=true;
  $('#free-space-out').innerHTML='<span class="spin"></span> 清理中…';
  try{
    const q=nuke?'?aggressive=true&drop_diag=1':'?aggressive=true';
    const d=await (await api('/admin/api/maintenance/cleanup'+q,
      {method:'POST'})).json();
    const extra=d.diag_db?`（含诊断库 ${fmtB(d.diag_db)}）`:'';
    $('#free-space-out').textContent=
      `已释放 ${fmtB(d.freed_bytes||0)}${extra}`;
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
  connectLive();
  try{ await Promise.all([loadDash(),loadConf()]) }catch(e){}
}
autoRefresh('dash-auto','dash-intv','#p-dash',loadDash);
boot();
