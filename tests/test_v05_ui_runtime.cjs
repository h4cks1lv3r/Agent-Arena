// Run with NODE_PATH pointing to a temporary jsdom installation. No network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(root + '/arena/static/index.html', 'utf8');
const app = fs.readFileSync(root + '/arena/static/app.js', 'utf8');
const state = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json,tempfile
from arena.service import Service
with tempfile.TemporaryDirectory() as directory:
 service=Service(directory,environ={})
 print(json.dumps(service.public_state()))
 service.meta.close();service.engine.close()
`], {cwd: root, encoding: 'utf8'}));
Object.assign(state.experiment, {mode:'paper', status:'ready', autopilot:true,
  auto_resume:true, monthly_model_budget:100, loss_limit_includes_model_costs:false, output_token_limit:8192});
state.agents[0].positions = {SPY:{qty:1,avg_price:100}, AAPL:{qty:1,avg_price:100}};
state.agents[1].positions = {MSFT:{qty:1,avg_price:100}};
state.agents[0].equity = 190; state.agents[0].trading_equity = 250;
state.agents[0].model_cost = 60; state.agents[0].trading_pnl = 0; state.agents[0].net_pnl = -60;
state.agent_controls.openai = {paused:false, quote_times:{SPY:new Date().toISOString(), AAPL:new Date(Date.now()-600000).toISOString(), OLD:new Date().toISOString()}};
state.agent_controls.claude = {paused:false, quote_times:{}};
state.connections = {openai:{alpaca:true,model:true,model_test_estimate:{reserved_usd:.10}},claude:{alpaca:true,model:true,model_test_estimate:{reserved_usd:.04}}};
state.autonomy.openai = {exit_retries:{SPY:{message:'Broker rejected the automatic exit.',next_retry_at:new Date(Date.now()+300000).toISOString()}}};
state.history = [
  {at:'2026-01-01T00:00:00Z',agents:{openai:250,claude:250}},
  {at:'2026-01-02T00:00:00Z',agents:{openai:125,claude:250}},
  {at:'2026-02-01T00:00:00Z',agents:{openai:190,claude:250}}
];
const dom = new JSDOM(html, {url:'http://127.0.0.1:8765',runScripts:'outside-only',pretendToBeVisual:true});
const w = dom.window, $ = id => w.document.getElementById(id);
const calls = [], intervals = [], errors = [];
w.addEventListener('error', e => errors.push(e.message));
w.setInterval = (fn,ms) => {intervals.push({fn,ms});return intervals.length};
w.confirm = () => true; w.HTMLElement.prototype.scrollIntoView = () => {};
w.fetch = async (url,options={}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({url,body});
  if(url==='/api/session')return {ok:true,status:200,json:async()=>({token:'local',remote:false})};
  if(url==='/api/test-connections'){
    state.server_job={id:'check-'+calls.length,kind:'control',action:'test-connections',status:'running',experiment_id:state.experiment.id};
    return {ok:true,status:202,json:async()=>({state:structuredClone(state),job:structuredClone(state.server_job)})};
  }
  if(url==='/api/auto-resume')state.experiment.auto_resume=body.enabled;
  if(url==='/api/config')Object.assign(state.experiment,body);
  if(url==='/api/halt'){state.experiment.status='halted';state.experiment.autopilot=false}
  return {ok:true,status:200,json:async()=>structuredClone(state)};
};
const wait = () => new Promise(resolve=>setTimeout(resolve,25));
const poll = async () => {intervals.find(item=>item.ms===3000).fn();await wait()};
(async()=>{
  try {
    w.eval(app);await wait();
    assert.equal($('app-content').hidden,false);
    assert.equal($('test-paid-model').checked,false,'Paid calls must require an explicit choice.');
    assert.match($('connection-test-estimate').textContent,/\$0\.1400/);
    assert.match($('quote-time').textContent,/1 holding price missing/);
    assert.match(w.document.querySelector('[data-agent-quote-time="openai"]').textContent,/stale/,'Fresh SPY and a closed OLD position must not hide stale AAPL.');
    assert.match(w.document.querySelector('[data-agent-quote-time="claude"]').textContent,/Missing: MSFT/);
    assert.match($('agent-grid').textContent,/Exit retry pending/);
    assert.match($('agent-grid').textContent,/Broker rejected the automatic exit/);
    const points=$('equity-chart').querySelector('polyline').getAttribute('points').split(' ').map(point=>point.split(',').map(Number));
    assert.equal(points.length,3);
    assert.ok(points[1][0]<100,'One day should occupy about1/31 of the full chart span, not half.');
    assert.equal($('config-form').elements.namedItem('output_token_limit').value,'8192');
    $('test-connections').click();await wait();
    assert.deepEqual(calls.find(call=>call.url==='/api/test-connections').body,{paid_model:false});
    assert.equal($('halt-button').disabled,false);
    assert.equal($('auto-resume-toggle').disabled,false);
    const toggle=$('auto-resume-toggle');toggle.checked=false;toggle.dispatchEvent(new w.Event('change',{bubbles:true}));await wait();
    assert.equal(state.experiment.auto_resume,false);
    assert.equal($('config-form').elements.namedItem('auto_resume').checked,false);
    state.server_job.status='complete';
    state.server_job.result={paid_model:false,at:new Date().toISOString(),checks:[{agent_id:'openai',name:'<img src=x onerror=alert(1)>',broker:{status:'ok',message:'No orders sent'},market_data:{status:'stale',message:'Sample valuation mark received; each order still needs its own fresh executable quote.',age_seconds:900,feed:'delayed_sip',source:'trade',execution_eligible:false},model:{status:'skipped',message:'Paid model check was not requested.'}}]};
    await poll();
    assert.match($('connection-test-result').textContent,/Broker checks only/);
    assert.match($('connection-test-result').textContent,/Broker account and clock: ok/);
    assert.match($('connection-test-result').textContent,/Market data: stale/);
    assert.match($('connection-test-result').textContent,/900 seconds · delayed_sip · trade/);
    assert.match($('connection-test-result').textContent,/valuation only; not an execution quote/);
    assert.match($('connection-test-result').textContent,/Paid model check was not requested/);
    assert.equal($('connection-test-result').querySelectorAll('img').length,0,'Diagnostic content must remain escaped.');
    $('test-paid-model').checked=true;$('test-paid-model').dispatchEvent(new w.Event('change',{bubbles:true}));
    assert.match($('test-connections').textContent,/paid model/);
    $('test-connections').click();await wait();
    assert.equal(calls.filter(call=>call.url==='/api/test-connections').at(-1).body.paid_model,true);
    state.server_job.status='complete';
    state.experiment.loss_limit_includes_model_costs=true;await poll();
    assert.equal($('loss-remaining').textContent,'$0.00');
    assert.match($('loss-detail').textContent,/includes model costs/);
    assert.deepEqual(errors,[]);
    console.log('PASS: v0.5 DOM controls, explicit paid diagnostics, server cost estimate, separate broker/data health, result escaping, restart opt-out during work, oldest/missing holding prices, exit retry status, loss policy, proportional full-period chart.');
  } finally {w.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
