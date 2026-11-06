VOICE_HTML = r'''
<style>@media(max-width:740px){.screenAndPtt{position:relative;top:auto;z-index:auto}}</style>
<section id="voicePresets" class="channelPanel">
  <h2>预制语音 · v8.3</h2>
  <label for="voiceChoice">选择语音内容</label>
  <select id="voiceChoice" style="width:100%" onchange="voiceSelect()"><option value="">请添加语音</option></select>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
    <button id="voiceSendButton" class="primary" onclick="voiceSend()" disabled>发送语音</button>
    <button id="voiceStopButton" onclick="voiceStop()" disabled>停止发送</button>
    <button onclick="voiceEdit()">编辑</button><button onclick="voiceNew()">添加</button>
    <button onclick="location.reload()">刷新页面</button>
  </div>
  <div id="voiceStatus" class="hint" role="status" aria-live="polite" style="margin:8px 0">选择内容后点击发送</div>
  <audio id="voicePreview" controls preload="metadata" style="width:100%" hidden></audio>
  <details id="voiceEditor"><summary style="cursor:pointer;padding:8px 0">添加或编辑语音</summary>
    <label for="voiceLabel">文字描述（显示在下拉菜单中）</label>
    <input id="voiceLabel" maxlength="60" style="width:100%" placeholder="例如：呼叫测试">
    <label for="voiceSource">语音来源</label>
    <select id="voiceSource" style="width:100%" onchange="voiceSourceChange()"><option value="tts">文字合成</option><option value="recording">手机直接录音</option></select>
    <div id="voiceTtsFields">
      <label for="voiceText">朗读正文</label><textarea id="voiceText" maxlength="500" rows="4" style="width:100%;font:inherit;padding:8px;box-sizing:border-box" placeholder="输入要合成并发射的文字"></textarea>
      <label for="voiceName">合成声音</label><select id="voiceName" style="width:100%"></select>
      <p class="hint">云希：联网中文男声。生成时将正文发送微软，生成后缓存到本机。</p>
      <label for="voiceRate">语速（0 正常，-3 慢，3 快）</label><input id="voiceRate" type="number" min="-3" max="3" value="0">
    </div>
    <div id="voiceRecordFields" hidden>
      <p class="hint">使用手机 App 0.2.7 及以上版本。录音时不发射，最长 60 秒；录完保存后可试听。</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap"><button id="voiceRecordStart" onclick="voiceRecordStart()">开始录音</button><button id="voiceRecordStop" onclick="voiceRecordStop()" disabled>结束录音</button><button id="voiceRecordSave" onclick="voiceRecordSave()" disabled>保存这段录音</button></div>
      <div id="voiceRecordStatus" role="status" class="hint" style="margin:8px 0">尚未录音</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"><button id="voiceSaveButton" onclick="voiceSave()">保存并生成语音</button><button id="voiceDeleteButton" onclick="voiceDelete()" disabled>删除此条</button></div>
    <div id="voiceEditStatus" class="hint" role="status" aria-live="polite" style="margin-top:8px">正文最多 500 字，单条语音最多 60 秒。</div>
  </details>
</section>
'''

VOICE_JS = r'''
let voiceItems=[],voiceEditing=null,voiceListSignature='',voiceBusy=false,voiceVoicesLoaded=false,voiceDefaultName='';
let voiceRecording=false,voiceRecorded=false,voiceRecordSaving=false,voicePollRunning=false;
function voiceMessage(e){$('voiceEditor').open=true;$('voiceEditStatus').textContent=e.message||String(e)}
function voiceCurrent(){return voiceItems.find(x=>x.id===$('voiceChoice').value)}
function voiceResult(item){
  if(item.status==='ready')return (item.source==='recording'?'录音已保存':'生成成功')+' · '+(item.duration||'')+' 秒，可试听和发送';
  if(item.status==='error')return '生成失败：'+item.error;
  if(item.status==='queued')return '等待前一条生成 · 已等待 '+item.elapsed+' 秒';
  if(item.status==='generating')return '正在生成 · 已等待 '+item.elapsed+' 秒（超过 90 秒会提示失败）';
  return '尚未生成音频，请保存并生成';
}
function voiceSelect(){
  const item=voiceCurrent(),ready=item&&item.status==='ready';
  $('voiceSendButton').disabled=voiceBusy||voiceRecording||!ready;
  const key=ready?item.id+':'+item.revision:'';
  if($('voicePreview').dataset.key!==key){
    $('voicePreview').pause();$('voicePreview').dataset.key=key;
    if(ready)$('voicePreview').src='/api/voice/presets/'+encodeURIComponent(item.id)+'/audio?v='+item.revision;
    else $('voicePreview').removeAttribute('src');
  }
  $('voicePreview').hidden=!ready;
  if(!voiceBusy)$('voiceStatus').textContent=item?voiceResult(item):'请添加语音内容';
}
async function voiceLoadVoices(){
  if(voiceVoicesLoaded)return;
  const data=await api('/api/voice/voices');
  $('voiceName').replaceChildren(...data.voices.map(v=>{const o=document.createElement('option');o.value=v.name;o.textContent=v.label||(v.name+' · '+v.culture+(v.gender==='Male'?' · 男声':v.gender==='Female'?' · 女声':''));return o}));
  const zh=data.voices.find(v=>v.culture==='zh-CN'&&v.gender==='Male')||data.voices.find(v=>v.culture==='zh-CN');if(zh){voiceDefaultName=zh.name;$('voiceName').value=zh.name}
  voiceVoicesLoaded=true;
}
async function voiceLoad(){
  const data=await api('/api/voice/presets');voiceItems=data.items;
  const signature=JSON.stringify(data.items.map(x=>({...x,elapsed:0})));
  if(signature!==voiceListSignature){
    const selected=$('voiceChoice').value;voiceListSignature=signature;
    $('voiceChoice').replaceChildren(...voiceItems.map(item=>{
      const o=document.createElement('option');o.value=item.id;
      o.textContent=item.label+' · '+(item.source==='recording'?'录音':'合成')+(item.status==='queued'?'（等待中）':item.status==='generating'?'（生成中）':item.status==='error'?'（失败）':'');return o;
    }));
    if(!voiceItems.length){const o=document.createElement('option');o.value='';o.textContent='请添加语音';$('voiceChoice').append(o)}
    if(voiceItems.some(x=>x.id===selected))$('voiceChoice').value=selected;
  }
  voiceSelect();
  if(voiceEditing){const current=voiceItems.find(x=>x.id===voiceEditing.id);if(current&&current.revision===voiceEditing.revision)$('voiceEditStatus').textContent=voiceResult(current)}
}
function voiceNativeRecording(){return typeof K1Audio!=='undefined'&&typeof K1Audio.startPresetRecording==='function'}
function voiceSourceChange(){
  const recording=$('voiceSource').value==='recording';
  $('voiceTtsFields').hidden=recording;$('voiceRecordFields').hidden=!recording;
  $('voiceSaveButton').textContent=recording?'保存文字描述':'保存并生成语音';
  $('voiceRecordStart').disabled=!voiceNativeRecording()||voiceRecording||voiceBusy;
  if(recording&&!voiceNativeRecording())$('voiceRecordStatus').textContent='当前 App 版本不支持直接录音，请安装 0.2.7 版后刷新页面';
  if(!recording)voiceLoadVoices().catch(voiceMessage);
}
async function voiceNew(){
  if(voiceRecording){voiceMessage('请先结束录音');return}
  voiceEditing=null;voiceRecorded=false;$('voiceRecordSave').disabled=true;
  $('voiceLabel').value='';$('voiceText').value='';$('voiceRate').value=0;$('voiceSource').value='tts';
  $('voiceDeleteButton').disabled=true;$('voiceEditor').open=true;
  $('voiceEditStatus').textContent='填写描述，选择文字合成或手机录音';voiceSourceChange();if(voiceDefaultName)$('voiceName').value=voiceDefaultName;$('voiceLabel').focus();
}
async function voiceEdit(){
  if(voiceRecording){voiceMessage('请先结束录音');return}
  const item=voiceCurrent();if(!item){await voiceNew();return}
  voiceEditing={...item};voiceRecorded=false;$('voiceRecordSave').disabled=true;
  $('voiceLabel').value=item.label;$('voiceText').value=item.text||'';$('voiceRate').value=item.rate||0;
  $('voiceSource').value=item.source||'tts';$('voiceDeleteButton').disabled=false;$('voiceEditor').open=true;
  voiceSourceChange();if(item.source!=='recording'){try{await voiceLoadVoices();$('voiceName').value=item.voice}catch(e){voiceMessage(e)}}
  $('voiceEditStatus').textContent=voiceResult(item);
}
async function voiceSave(){
  $('voiceSaveButton').disabled=true;
  try{
    const source=$('voiceSource').value;
    if(source==='tts')await voiceLoadVoices();
    const body={source,label:$('voiceLabel').value,text:$('voiceText').value,voice:$('voiceName').value,rate:Number($('voiceRate').value)};
    if(voiceEditing){body.id=voiceEditing.id;body.revision=voiceEditing.revision}
    voiceEditing=await api('/api/voice/presets',{method:'POST',body:JSON.stringify(body)});
    $('voiceDeleteButton').disabled=false;await voiceLoad();$('voiceChoice').value=voiceEditing.id;voiceSelect();
  }catch(e){voiceMessage(e)}finally{$('voiceSaveButton').disabled=false}
}
async function voiceDelete(){
  if(!voiceEditing)return;
  if(voiceRecording){voiceMessage('请先结束录音');return}
  try{await api('/api/voice/presets/'+encodeURIComponent(voiceEditing.id),{method:'DELETE',body:JSON.stringify({revision:voiceEditing.revision})});await voiceLoad();await voiceNew();$('voiceEditStatus').textContent='已删除'}catch(e){voiceMessage(e)}
}
async function voiceSend(){
  const item=voiceCurrent();if(!item||pttHeld||voiceRecording)return;
  $('voicePreview').pause();$('voiceSendButton').disabled=true;
  try{await api('/api/voice/send',{method:'POST',body:JSON.stringify({id:item.id})});await voicePoll()}catch(e){$('voiceStatus').textContent='发送失败：'+e.message;$('voiceSendButton').disabled=false}
}
async function voiceStop(){try{await api('/api/voice/stop',{method:'POST',body:'{}'});await voicePoll()}catch(e){$('voiceStatus').textContent=e.message}}
function voiceRecordStart(){
  if(!voiceNativeRecording()||voiceBusy||pttHeld||voiceRecording)return;
  voiceRecorded=false;$('voiceRecordSave').disabled=true;$('voiceRecordStatus').textContent='正在启动手机麦克风…';
  K1Audio.startPresetRecording();
}
function voiceRecordStop(){if(voiceNativeRecording())K1Audio.stopPresetRecording()}
function voiceRecordSave(){
  if(!voiceRecorded||voiceRecording||voiceRecordSaving)return;
  const label=$('voiceLabel').value.trim();if(!label){voiceMessage('请先填写录音的文字描述');return}
  voiceRecordSaving=true;$('voiceRecordSave').disabled=true;$('voiceRecordStatus').textContent='正在上传并保存录音…';
  K1Audio.savePresetRecording(label,voiceEditing?voiceEditing.id:'',voiceEditing?voiceEditing.revision:0);
}
window.k1PresetRecordingStatus=(state,seconds,message)=>{
  voiceRecording=state==='recording'||state==='starting';
  if(state==='ready')voiceRecorded=true;
  if(state==='error'){voiceRecordSaving=false;voiceRecorded=false}
  if(state==='save_error'){voiceRecordSaving=false;voiceRecorded=true}
  $('voiceRecordStart').disabled=voiceRecording||!voiceNativeRecording();
  $('voiceRecordStop').disabled=!voiceRecording;
  $('voiceRecordSave').disabled=!voiceRecorded||voiceRecording||voiceRecordSaving;
  $('voiceRecordStatus').textContent=message||((voiceRecording?'录音中 · ':'录音完成 · ')+seconds.toFixed(1)+' 秒');
  voiceSelect();
};
window.k1PresetRecordingSaved=async item=>{
  voiceRecordSaving=false;voiceRecorded=false;voiceEditing=item;
  $('voiceRecordSave').disabled=true;$('voiceDeleteButton').disabled=false;
  $('voiceRecordStatus').textContent='录音保存成功，可试听和发送';
  await voiceLoad();$('voiceChoice').value=item.id;voiceSelect();
  $('voiceEditStatus').textContent='录音保存成功；可修改描述后点击“保存文字描述”';
};
async function voicePoll(){
  if(voicePollRunning)return;voicePollRunning=true;
  try{
    const s=await api('/api/voice/state');voiceBusy=s.active;$('voiceStopButton').disabled=!s.active;
    await voiceLoad();
    if(s.active)$('voiceStatus').textContent='正在发射：'+s.label+' · '+s.progress+'%';
    else if(s.error)$('voiceStatus').textContent='发送失败：'+s.error;
    else if(s.progress===100)$('voiceStatus').textContent='发送完成，PTT 已释放';
  }finally{voicePollRunning=false}
}
voiceLoad().catch(voiceMessage);setInterval(()=>voicePoll().catch(e=>{$('voiceStatus').textContent='状态更新失败：'+e.message}),1000);
'''
