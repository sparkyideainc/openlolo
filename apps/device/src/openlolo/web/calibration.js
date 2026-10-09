'use strict';
let trial=null, pending=false;
const geometry=()=>({width:innerWidth,height:innerHeight,scale:visualViewport?.scale||1});
async function event(data){const r=await fetch('events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...geometry(),...data})});if(!r.ok)throw new Error('Session expired or viewport changed. Restart calibration.');}
async function poll(){try{const r=await fetch('state',{cache:'no-store'});if(!r.ok)throw new Error('Session expired');const s=await r.json();trial=s.trial;pending=s.pending;const t=document.getElementById('target');t.hidden=!s.target;if(s.target){t.style.left=s.target[0]*innerWidth+'px';t.style.top=s.target[1]*innerHeight+'px';t.textContent=trial+1;}document.getElementById('label').textContent=s.target?`${trial<9?'Training':trial<14?'Held-out':'Grid'} target ${trial+1}/${s.total} · ${pending?'Awaiting tap':'Use “Send next target” on Mac'}`:'Complete. Save the result on the Mac.';}catch(e){document.getElementById('label').textContent=e.message;}}
window.addEventListener('pointerdown',e=>{e.preventDefault();const m=document.getElementById('marker');m.style.left=e.clientX+'px';m.style.top=e.clientY+'px';event({type:'down',x:e.clientX,y:e.clientY,trial}).then(poll).catch(e=>document.getElementById('label').textContent=e.message);});
window.addEventListener('resize',()=>event({type:'geometry'}).catch(e=>document.getElementById('label').textContent=e.message));
event({type:'geometry'}).then(poll).catch(e=>document.getElementById('label').textContent=e.message);setInterval(poll,300);
