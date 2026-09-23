// keys
let KEY_MAP={};
async function loadKeys(){
  const d=await (await api('/admin/api/keys')).json();
  KEY_MAP={}; d.keys.forEach(k=>KEY_MAP[k.id]=k);
  $('#key-body').innerHTML=d.keys.map(k=>`<tr><td>${k.id}</td><td>${esc(k.name)}</td>
    <td>${esc(k.prefix)}…${esc(k.tail)}</td>
    <td class="muted">${esc(k.models||'全部')}</td>
    <td>${k.max_concurrent||'∞'}</td>
    <td>${fmtT(k.created)}</td><td>${fmtT(k.last_used)}</td>
    <td><span class="tag ${k.disabled?'off':'ok'}">${k.disabled?'禁用':'启用'}</span></td>
    <td style="white-space:nowrap"><button onclick="editKey(${k.id})">限制</button>
        <button onclick="toggleKey(${k.id},${k.disabled?0:1})">${k.disabled?'启用':'禁用'}</button>
        <button class="danger" onclick="delKey(${k.id},'${esc(k.name)}')">删除</button></td></tr>`).join('')||'<tr><td colspan=9 class=muted>暂无 key</td></tr>';
}
async function createKey(){
  const name=$('#key-name').value.trim();if(!name)return toast('请输入名称');
  const d=await (await api('/admin/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,models:$('#key-models').value.trim()||null,
      max_concurrent:parseInt($('#key-conc').value)||0})})).json();
  $('#key-name').value='';$('#key-models').value='';$('#key-conc').value='';
  $('#key-reveal').innerHTML=`<div class="reveal">⚠️ 完整 key 只显示这一次，请立即保存：<br><b>${esc(d.key)}</b>
    <a style="margin-left:10px" onclick="navigator.clipboard.writeText('${d.key}');toast('已复制')">复制</a></div>`;
  loadKeys();
}
async function editKey(id){
  const k=KEY_MAP[id];if(!k)return;
  const models=prompt('允许的模型（逗号分隔 uid/别名，留空 = 全部）',k.models||'');
  if(models===null)return;
  const mc=prompt('并发上限（0 = 不限）',k.max_concurrent||0);
  if(mc===null)return;
  await api('/admin/api/keys/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({models,max_concurrent:parseInt(mc)||0})});
  toast('已更新');loadKeys();
}
async function toggleKey(id,dis){await api('/admin/api/keys/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({disabled:!!dis})});loadKeys()}
async function delKey(id,name){
  if(!confirm(`删除 key「${name}」？使用它的客户端会立即失效。`))return;
  await api('/admin/api/keys/'+id,{method:'DELETE'});loadKeys();
}
