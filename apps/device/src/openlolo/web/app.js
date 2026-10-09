'use strict';
const $ = id => document.getElementById(id);
let lease = null, frame = null, activeOperation = null, calibration = null, imageURL = null, busy = false;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function message(error) { $('message').textContent = error.message || String(error); $('message').classList.add('visible'); }
function clearMessage() { $('message').classList.remove('visible'); }
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST',
    headers: data === undefined ? {} : {'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) showLogin(); throw new Error(result.error?.code + ': ' + result.error?.message); }
  return result;
}
function showLogin() { $('login-panel').hidden = false; $('workspace').hidden = true; lease = null; frame = null; }
async function control(command, extra = {}) {
  const result = await api('control/' + command, {operation_id: crypto.randomUUID(), lease_token: lease, ...extra});
  if (result.control?.lease_token) lease = result.control.lease_token;
  if (command === 'release') lease = null;
  return result;
}
async function status() {
  const result = await api('status');
  $('login-panel').hidden = true; $('workspace').hidden = false;
  $('device').textContent = `${result.backend === 'simulated' ? 'SIMULATOR · ' : ''}${result.device || 'Device binding required'}${result.transport ? ' · ' + result.transport.toUpperCase() : ''}`;
  const conn = result.connection || {};
  $('connection').textContent = `${conn.state || 'unknown'} · ${conn.message || ''}${conn.last_error?.code ? ' · ' + conn.last_error.code : ''}${conn.transport ? ' · via ' + conn.transport.toUpperCase() : ''}${conn.wifi_paired === false ? ' · Wi-Fi pairing missing' : ''}${conn.hid_active ? ' · input session open' : ''}`;
  $('connection').className = conn.state === 'connected' ? 'good' : 'bad';
  if (result.transport) $('transport').value = result.transport;
  const c = result.control;
  if (!c.owned) lease = null;
  $('lease').textContent = `${c.paused ? 'PAUSED · ' : ''}${c.owned ? 'You have control' : c.active ? 'Another session has control' : 'No control lease'} · ${Math.ceil(c.remaining_seconds)}s remaining · ${c.queue_depth} queued${c.release_error ? ' · release unconfirmed' : ''}`;
  $('health').replaceChildren();
  for (const [name, value] of Object.entries(result.components)) {
    const row = document.createElement('div'); row.className = 'health-row';
    const title = document.createElement('span'); title.textContent = name;
    const state = document.createElement('span'); state.textContent = value.connection ? `${value.state} · ${value.connection.state}` : value.state || `Wi-Fi ${value.wifi?.join(', ')} · internet ${value.internet}`;
    state.className = value.state === 'ready' ? 'good' : 'bad'; row.append(title, state); $('health').append(row);
  }
  if (result.setup_required) $('guidance').textContent = 'Bind the owner-confirmed phone identity and transport with the local CLI. See the setup runbook.';
  if (result.profile.calibration) $('cal-result').textContent = JSON.stringify(result.profile.calibration, null, 2);
  const caps = await api('capabilities'); $('shortcuts').replaceChildren(); $('buttons').replaceChildren();
  for (const name of caps.shortcuts) { const button = document.createElement('button'); button.textContent = name.replaceAll('_', ' '); button.onclick = () => perform({kind:'shortcut', shortcut:name}).catch(message); $('shortcuts').append(button); }
  for (const name of caps.buttons || []) { const button = document.createElement('button'); button.textContent = name.replaceAll('_', ' '); button.onclick = () => perform({kind:'button', button:name}).catch(message); $('buttons').append(button); }
}
async function screenshot() {
  $('refresh').disabled = true;
  try {
    const result = await api('screenshot', {}); frame = result.metadata;
    const bytes = Uint8Array.from(atob(result.image), c => c.charCodeAt(0));
    if (imageURL) URL.revokeObjectURL(imageURL);
    imageURL = URL.createObjectURL(new Blob([bytes], {type:frame.media_type}));
    $('screen').src = imageURL; $('screen').hidden = false; $('empty').hidden = true;
    $('frame-info').textContent = `${frame.width} × ${frame.height} · ${frame.request_seconds.toFixed(2)}s request · source age unknown · received ${new Date(frame.received_at*1000).toLocaleTimeString()}`;
  } catch (error) { frame = null; $('screen').hidden = true; $('empty').hidden = false; $('empty').textContent = 'Capture failed. Refresh to obtain a new frame.'; throw error; }
  finally { $('refresh').disabled = false; }
}
function logOperation(operation) {
  let entry = document.getElementById('op-' + operation.id);
  if (!entry) { entry = document.createElement('li'); entry.id = 'op-' + operation.id; $('operations').prepend(entry); }
  entry.textContent = `${operation.kind} · ${operation.state} · ${operation.id.slice(0,8)}${operation.result?.code ? ' · ' + operation.result.code : ''}`;
  while ($('operations').children.length > 50) $('operations').lastChild.remove();
}
async function finish(operation) {
  activeOperation = operation.id;
  try {
    logOperation(operation);
    while (['QUEUED','DISPATCHED'].includes(operation.state)) { await sleep(200); operation = await api('operations/' + operation.id); logOperation(operation); }
    if (operation.state !== 'SUCCEEDED') throw new Error(`${operation.state}: ${operation.result?.code || 'Check the phone before issuing another action. This operation will not be replayed.'}`);
    return operation.result;
  } finally { activeOperation = null; }
}
async function perform(action) {
  if (busy) throw new Error('Wait for the current action to finish.');
  if (!lease) throw new Error('Acquire control first.');
  busy = true; clearMessage();
  try { await finish(await api('actions', {operation_id:crypto.randomUUID(), lease_token:lease, action})); await screenshot(); }
  finally { busy = false; await status(); }
}
async function setup(kind, payload={}) {
  if (!lease) throw new Error('Acquire control first.');
  return finish(await api('setup/' + kind, {operation_id:crypto.randomUUID(), lease_token:lease, payload}));
}
function bind(id, callback) { $(id).addEventListener('click', () => { clearMessage(); Promise.resolve().then(callback).catch(message); }); }
$('login').onsubmit = async e => { e.preventDefault(); try { await api('login', {credential:$('credential').value}); $('credential').value=''; clearMessage(); await status(); } catch(error) { message(error); } };
bind('refresh', screenshot); bind('acquire', async()=>{await control('acquire'); await status();});
bind('release', async()=>{await control('release'); await status();}); bind('resume', async()=>{await control('resume'); await status();});
bind('stop', async()=>{await control('pause'); await status();}); bind('cancel', async()=>{if(activeOperation) await control('cancel',{target:activeOperation});});
bind('logout', async()=>{await api('logout',{}); if(imageURL) URL.revokeObjectURL(imageURL); $('screen').removeAttribute('src'); showLogin();});
bind('recover', async()=>{await setup('recover'); await status();});
bind('switch-transport', async()=>{await setup('transport', {transport:$('transport').value}); await status();});
bind('unbind', async()=>{if(!confirm('Forget the bound phone and all pairing records? Setup over USB is required again.')) return; await setup('unbind'); await status();});
bind('apps', async()=>{const result=await api('apps'); $('info').textContent=result.apps.map(a=>`${a.name || '?'} · ${a.bundle_id}${a.version ? ' · ' + a.version : ''}`).join('\n') || 'No apps reported.';});
bind('device-info', async()=>{$('info').textContent=JSON.stringify(await api('device'), null, 2);});
$('typing').onsubmit = e => {e.preventDefault(); const text=$('text').value; if(!/^[\x20-\x7e]*$/.test(text)) return message(new Error('Only printable U.S. ASCII is supported. Nothing was sent.')); perform({kind:'type_text',text}).then(()=>$('text').value='').catch(message);};
$('keys').onsubmit = e => {e.preventDefault(); perform({kind:'key',key:$('key').value,modifiers:[...document.querySelectorAll('.modifiers input:checked')].map(e=>e.value)}).catch(message);};
function point(e) { const r=$('screen').getBoundingClientRect(); return {x:Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),y:Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))}; }
let down=null;
$('screen').onpointerdown = e => {if(!frame) return; e.preventDefault(); down={...point(e),frame:frame.frame_id,geometry:frame.geometry_epoch}; $('screen').setPointerCapture(e.pointerId);};
$('screen').onpointercancel = () => {down=null;};
$('screen').onpointerup = e => {if(!down || !frame) return; const start=down; down=null; const end=point(e); const drag=Math.hypot(end.x-start.x,end.y-start.y)>.015; const kind=drag?'swipe':$('pointer-mode').value; perform({kind,x:start.x,y:start.y,...(drag?{x2:end.x,y2:end.y}:{}),frame_id:start.frame,geometry_epoch:start.geometry,seconds:Number($('seconds').value)}).catch(message);};
bind('cal-start', async()=>{if(!frame) throw new Error('Capture a portrait screenshot first.'); calibration=await setup('calibration_start',{native_width:frame.native_width,native_height:frame.native_height,viewport_top:Number($('viewport-top').value)}); const link=document.createElement('a');link.href=calibration.url;link.textContent='Open on the iPhone: '+calibration.url;link.rel='noreferrer';$('cal-link').replaceChildren(link);});
bind('cal-next', async()=>{await setup('calibration_next'); await screenshot();});
bind('cal-finish', async()=>{if(!calibration) throw new Error('Start calibration first.'); const result=await setup('calibration_finish',{session:calibration.session});$('cal-result').textContent=JSON.stringify(result,null,2);});
setInterval(()=>{if(lease) control('renew').catch(error=>{lease=null;message(error);});},15000);
setInterval(()=>{if(!$('workspace').hidden) status().catch(message);},5000);
status().catch(()=>showLogin());
