// Operator-confirmed unknown-order recovery UI. Run with temporary jsdom via NODE_PATH.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const state=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c',`
import json,tempfile
from arena.service import Service
with tempfile.TemporaryDirectory() as d:
 s=Service(d,environ={});print(json.dumps(s.public_state()));s.meta.close();s.engine.close()
`],{cwd:root,encoding:'utf8'}));
Object.assign(state.experiment,{mode:'paper',status:'ready',autopilot:true});
state.agent_controls.openai={paused:true,pause_kind:'unknown_order'};
state.orders=[{id:'order-1',client_order_id:'arena-original-id',agent_id:'openai',status:'unknown',side:'buy',symbol:'SPY',notional:25,reserved:25,filled_qty:0}];
const dom=new JSDOM(fs.readFileSync(root+'/arena/static/index.html','utf8'),{url:'http://127.0.0.1:8765',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,$=id=>w.document.getElementById(id),calls=[],intervals=[],errors=[];
w.addEventListener('error',e=>errors.push(e.message));
w.setInterval=(fn,ms)=>{intervals.push({fn,ms});return intervals.length};
w.HTMLElement.prototype.scrollIntoView=()=>{};
w.confirm=()=>true;
w.fetch=async(url,options={})=>{
 const body=options.body?JSON.parse(options.body):null;calls.push({url,body,headers:options.headers});
 if(url==='/api/session')return{ok:true,status:200,json:async()=>({token:'test-csrf',remote:false})};
 if(url==='/api/orders/resolve-unknown'){
  state.server_job={id:'resolve-1',kind:'control',action:'orders/resolve-unknown',status:'queued',experiment_id:state.experiment.id};
  return{ok:true,status:202,json:async()=>({state:structuredClone(state),job:structuredClone(state.server_job)})};
 }
 return{ok:true,status:200,json:async()=>structuredClone(state)};
};
const wait=()=>new Promise(resolve=>setTimeout(resolve,25));
const poll=async()=>{intervals.find(x=>x.ms===3000).fn();await wait()};
const submit=()=>$('order-resolution-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
(async()=>{try{
 w.eval(fs.readFileSync(root+'/arena/static/app.js','utf8'));await wait();
 assert.equal($('order-resolution-panel').hidden,true);
 w.document.querySelector('[data-resolve-order]').click();
 assert.equal($('order-resolution-panel').hidden,false);
 assert.equal($('resolution-client-id').value,'arena-original-id');
 assert.match($('order-resolution-detail').textContent,/SPY/);
 $('resolution-confirmation').value='Alpaca case 12345; team confirmed original order was never accepted.';
 submit();await wait();
 assert.equal(calls.filter(x=>x.url==='/api/orders/resolve-unknown').length,0,'No attestation must mean no mutation.');
 $('resolution-attestation').checked=true;submit();await wait();
 const mutation=calls.find(x=>x.url==='/api/orders/resolve-unknown');
 assert.deepEqual(mutation.body,{experiment_id:state.experiment.id,agent_id:'openai',order_id:'order-1',client_order_id:'arena-original-id',broker_confirmation:$('resolution-confirmation').value,confirmed_not_accepted:true});
 assert.equal(mutation.headers['X-Arena-Token'],'test-csrf');
 assert.equal($('resolution-submit').disabled,true);
 assert.equal($('halt-button').disabled,false,'Emergency halt must remain available during verification.');
 state.orders[0].status='rejected';state.orders[0].reserved=0;state.server_job.status='complete';
 await poll();
 assert.equal($('order-resolution-panel').hidden,true);
 assert.equal($('resolution-confirmation').value,'');
 assert.equal(state.agent_controls.openai.paused,true,'Resolution must not imply resume.');
 state.orders[0].status='unknown';state.orders[0].reserved=25;await poll();
 w.document.querySelector('[data-resolve-order]').click();
 state.experiment.id='different-experiment';await poll();
 assert.equal($('order-resolution-panel').hidden,true);
 state.experiment.trading_style='balanced';
 state.experiment.day_trade_guard={enabled:true,strict:true};
 await poll();
 assert.equal($('strict-swing-row').hidden,false,'Saved stock protection must stay visible in Balanced mode.');
 assert.equal($('strict-swing-toggle').checked,true);
 assert.match($('trading-style-detail').textContent,/protection remains active/);
 $('trading-style-button').click();await wait();
 assert.equal(calls.filter(x=>x.url==='/api/trading-style').at(-1).body.style,'fast_swing_strict','Changing pace must retain strict protection.');
 $('strict-swing-toggle').checked=false;
 $('strict-swing-toggle').dispatchEvent(new w.Event('change',{bubbles:true}));await wait();
 assert.deepEqual(calls.filter(x=>x.url==='/api/trading-style').at(-1).body,{style:'balanced',strict_same_day:false},'Changing strictness must retain Balanced pace.');
 assert.deepEqual(errors,[]);
 console.log('PASS: broker-confirmation UI requires attestation, binds exact experiment/client ID, uses CSRF and background job, keeps halt available, clears resolved/stale forms.');
}finally{w.close()}})().catch(e=>{console.error(e);process.exitCode=1});
