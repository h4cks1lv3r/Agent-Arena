const assert=require('node:assert/strict');
const fs=require('node:fs');
// Optional UI regression check: install jsdom in a temporary Node prefix and set NODE_PATH.
const {JSDOM}=require('jsdom');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(root+'/arena/static/index.html','utf8');
const app=fs.readFileSync(root+'/arena/static/app.js','utf8');
let state=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c',`
import json,tempfile
from arena.service import Service
with tempfile.TemporaryDirectory() as directory:
 service=Service(directory,environ={})
 print(json.dumps(service.public_state()))
 service.meta.close();service.engine.close()
`],{cwd:root,encoding:'utf8'}));
state.experiment.mode='paper';state.experiment.status='ready';state.experiment.autopilot=true;
state.connections={openai:{alpaca:true,model:true},claude:{alpaca:true,model:true}};
state.agents[0].model_cost=60;state.agents[0].equity=190;state.agents[0].net_pnl=-60;
state.agents[0].trading_pnl=0;state.agents[0].trading_equity=250;
state.experiment.monthly_model_budget=100;
state.agent_controls.openai={paused:true,pause_kind:'transient',reason:'Temporary timeout',recovery:{last_error:'Read timed out',next_retry_at:new Date().toISOString()}};
const dom=new JSDOM(html,{url:'http://127.0.0.1:8765',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,$=id=>w.document.getElementById(id);
const calls=[],intervals=[],errors=[];
w.addEventListener('error',e=>errors.push(e.message));
w.setInterval=(fn,ms)=>{intervals.push({fn,ms});return intervals.length};
w.confirm=()=>true;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(path,options={})=>{
 calls.push(path);
 if(path==='/api/session')return {ok:true,status:200,json:async()=>({token:'test-token',remote:false})};
 if(path==='/api/state')return {ok:true,status:200,json:async()=>structuredClone(state)};
 if(path==='/api/cycle'||path==='/api/reconcile'){
  state.server_job={id:path+'1',kind:path==='/api/cycle'?'cycle':'control',action:path.slice(5),status:'running'};
  return {ok:true,status:202,json:async()=>({state:structuredClone(state),job:structuredClone(state.server_job)})};
 }
 if(path==='/api/autopilot')state.experiment.autopilot=JSON.parse(options.body).enabled;
 if(path==='/api/halt'){state.experiment.autopilot=false;state.experiment.status='halted'}
 if(path==='/api/agent/halt'){const id=JSON.parse(options.body).agent_id;state.agent_controls[id]={paused:true,pause_kind:'operator',reason:'Operator paused'}}
 return {ok:true,status:200,json:async()=>structuredClone(state)};
};
const wait=()=>new Promise(r=>setTimeout(r,20));
const poll=async()=>{intervals.find(x=>x.ms===3000).fn();await wait()};
(async()=>{
 try{
  w.eval(app);await wait();
  assert.equal($('app-content').hidden,false);
  assert.equal(w.document.querySelectorAll('.scorecard').length,2);
  assert.match($('competition-scoreboard').textContent,/Recovering connection/);
  assert.equal($('loss-remaining').textContent,'$50.00','Model costs must not consume trading loss budget');
  assert.match($('loss-detail').textContent,/separate budget/);
  assert.match($('total-equity').textContent,/440/);
  const recoveryStop=w.document.querySelector('[data-agent-halt="openai"]');
  assert.equal(recoveryStop.disabled,false);recoveryStop.click();await wait();
  assert.match($('competition-scoreboard').textContent,/Agent paused/);
  $('cycle-button').click();await wait();
  assert.equal($('halt-button').disabled,false);
  assert.equal($('refresh-button').disabled,false);
  assert.equal($('reconcile-button').disabled,true);
  assert.equal($('new-experiment').disabled,true);
  assert.equal($('autopilot-toggle').disabled,false);
  assert.match($('notice-banner').textContent,/accepted/);
  assert.match($('job-status').textContent,/CYCLE · RUNNING/);
  const toggle=$('autopilot-toggle');toggle.checked=false;toggle.dispatchEvent(new w.Event('change',{bubbles:true}));await wait();
  assert.equal(state.experiment.autopilot,false);assert.ok(calls.includes('/api/autopilot'));
  $('halt-button').click();await wait();
  assert.equal(state.experiment.status,'halted');
  assert.match($('system-status').textContent,/HALTED/);
  state.server_job.status='complete';await poll();
  $('reconcile-button').click();await wait();
  assert.match($('job-status').textContent,/RECONCILE · RUNNING/);
  assert.doesNotMatch($('notice-banner').textContent,/completed/);
  state.server_job.status='error';state.server_job.error='Account work is in progress. This command did not run.';await poll();
  assert.match($('error-banner').textContent,/did not run/);
  $('two-agent-preset').click();
  assert.equal(w.document.querySelector('[data-agent-id="claude"] [data-field="model"]').value,'claude-opus-5-5');
  assert.equal(w.document.querySelector('[data-agent-id="claude"] [data-field="input_price"]').value,'4');
  assert.equal(w.document.querySelector('[data-agent-id="claude"] [data-field="output_price"]').value,'20');
  assert.deepEqual(errors,[]);
  console.log('PASS: actual JS DOM runtime: recovery display/control, loss budget excludes model fees, nonblocking accepted jobs, halt/off/refresh remain available, conflicts disabled, async failure displayed, explicit Claude5.5 preset.');
 }finally{w.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
