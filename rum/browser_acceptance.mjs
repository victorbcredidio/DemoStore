// Real browser acceptance against a running DemoStore with DEMO_RUM_ENABLED=1 (and backend
// telemetry on, so responses carry X-Trace-Id). Uses a disposable profile; never ignores TLS errors.
//   RUM_BASE_URL=http://127.0.0.1:8080 RUM_CHROME_PATH=/usr/bin/google-chrome node browser_acceptance.mjs
// RUM_CANDIDATE=1 instead tests a locally built ./dist bundle against a store without RUM.
import {chromium} from 'playwright-core';
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
const base=process.env.RUM_BASE_URL||'http://127.0.0.1:8080';
const candidate=process.env.RUM_CANDIDATE==='1';
const bundle=candidate?await readFile(new URL('./dist/rum-v1.js',import.meta.url),'utf8'):null;
const browser=await chromium.launch({headless:true,executablePath:process.env.RUM_CHROME_PATH||'/usr/bin/google-chrome'});
const checks=[],exports=[],requests=[],pageErrors=[];
let prior=null, page;
const report={started_at:new Date().toISOString(),candidate,base_url:base,checks};
function decodeAttributes(attrs=[]) {return Object.fromEntries(attrs.map(a=>[a.key,Object.values(a.value)[0]]));}
function spans() {return exports.flatMap(e=>(e.body.resourceSpans||[]).flatMap(r=>(r.scopeSpans||[]).flatMap(s=>s.spans||[])));}
async function flush(){await page.evaluate(()=>window.DemoStoreRUM.flush());await page.waitForTimeout(300);}
async function setMode(mode,delay_ms=1000){return page.evaluate(async ({mode,delay_ms})=>{
  const token=document.querySelector('meta[name="csrf-token"]').content;
  return (await fetch('/api/lab/control',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({mode,delay_ms})})).json();
},{mode,delay_ms});}
try {
  const ctx=await browser.newContext({viewport:{width:1100,height:1000}});
  page=await ctx.newPage();
  page.on('pageerror',e=>pageErrors.push(e.name));
  page.on('request',request=>{
    if(request.url().includes('/__rum__/v1/')){
      const data=request.postData(); if(data)exports.push({path:new URL(request.url()).pathname,body:JSON.parse(data)});
    }
    if(new URL(request.url()).pathname==='/api/checkout')requests.push({traceparent:request.headers().traceparent});
  });
  page.on('response',async response=>{
    if(new URL(response.url()).pathname==='/api/checkout'){
      const match=requests.find(r=>r.traceparent===response.request().headers().traceparent);
      if(match) {match.status=response.status();match.server_trace_id=response.headers()['x-trace-id'];}
    }
    if(response.url().includes('/__rum__/v1/')){
      const match=exports.find(e=>e.path===new URL(response.url()).pathname&&!e.http_status);
      if(match)match.http_status=response.status();
    }
  });
  if(candidate)await page.route('**/*',async intercepted=>{
    const request=intercepted.request(),path=new URL(request.url()).pathname;
    if(path==='/__rum__/rum-v1.js')return intercepted.fulfill({contentType:'application/javascript',body:bundle});    if(path.startsWith('/__rum__/v1/'))return intercepted.fulfill({status:200,contentType:'application/json',body:'{}'});
    if(request.resourceType()==='document'){
      const response=await intercepted.fetch();
      return intercepted.fulfill({response,body:(await response.text()).replace('<head>','<head><script src="/__rum__/rum-v1.js" defer></script>')});
    }
    return intercepted.continue();
  });
  await page.goto(base+'/login?traffic=synthetic');
  await page.waitForFunction(()=>window.DemoStoreRUM);
  assert.equal(await page.evaluate(()=>window.DemoStoreRUM.status().traffic_type),'synthetic');
  assert.equal(await page.locator('script[src^="/__rum__/rum-v1.js"]').count(),1);
  checks.push('observer starts once and recognizes explicit synthetic login');
  await page.locator('#login-username').fill(process.env.DEMO_USERNAME||'demo_user');
  await page.locator('#login-password').fill(process.env.DEMO_PASSWORD||'demo_pass');
  await page.locator('#login-submit').click();
  await page.waitForSelector('#signed-in-user');
  await page.waitForFunction(()=>window.DemoStoreRUM);
  prior=await page.evaluate(async()=> (await fetch('/api/lab/control')).json());
  await setMode('normal');
  await page.locator('#add-laptop').click();
  await page.waitForFunction(()=>document.querySelector('#cart-count').textContent==='1 item');
  await page.locator('#checkout-button').click();
  await page.waitForFunction(()=>document.querySelector('#checkout-result').dataset.state==='success');
  await page.waitForFunction(()=>window.DemoStoreRUM.status().recent.some(e=>e.name==='browser.checkout.visible_result'));
  await flush();
  assert(requests[0].traceparent);
  assert.equal(requests[0].server_trace_id,requests[0].traceparent.split('-')[1]);
  const http=spans().find(s=>s.name==='POST /api/checkout');
  assert(http&&http.parentSpanId);
  const parent=spans().find(s=>s.spanId===http.parentSpanId);
  assert.equal(parent?.name,'browser.checkout');
  checks.push('normal checkout visible result and browser HTTP/action parent; backend returns the same trace ID');
  await page.locator('#lab-controls-toggle').click();
  await page.locator('#rum-stall').click();
  await page.waitForFunction(()=>window.DemoStoreRUM.status().recent.some(e=>e.name==='browser.long_task'));
  await page.locator('#rum-shift').click();
  await page.waitForSelector('#rum-layout-demo');
  await page.locator('#rum-error').click();
  await page.waitForFunction(()=>window.DemoStoreRUM.status().errors>=1);
  await page.locator('#rum-rejection').click();
  await page.waitForFunction(()=>window.DemoStoreRUM.status().errors>=2);
  checks.push('browser-only stall, delayed layout shift, sanitized exception and rejection');
  if(process.env.RUM_SCREENSHOT_PATH)await page.screenshot({path:process.env.RUM_SCREENSHOT_PATH,fullPage:true});
  await page.setViewportSize({width:390,height:1100});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.setViewportSize({width:1100,height:1000});
  checks.push('browser controls fit a 390-pixel viewport');
  await setMode('slow',1200);
  await page.locator('#add-headphones').click();
  await page.waitForFunction(()=>document.querySelector('#cart-count').textContent==='1 item');
  await page.locator('#checkout-button').click();
  await page.waitForFunction(()=>document.querySelector('#checkout-result').dataset.state==='success'&&Number(document.querySelector('#last-checkout').dataset.durationMs)>=1100);
  checks.push('backend delay measured by browser checkout');
  await setMode('error');
  await page.locator('#add-laptop').click();
  await page.waitForFunction(()=>document.querySelector('#cart-count').textContent==='1 item');
  await page.locator('#checkout-button').click();
  await page.waitForFunction(()=>document.querySelector('#checkout-result').dataset.state==='error');
  await flush();
  assert(requests.some(r=>r.status===500));
  checks.push('HTTP 500 and failed visible checkout retained');
  await setMode('recovery');
  // Block only telemetry: shopping must continue even when collection fails.
  if(!candidate)await page.route('**/__rum__/v1/**',route=>route.abort());
  await page.locator('#checkout-button').click();
  await page.waitForFunction(()=>document.querySelector('#checkout-result').dataset.state==='success');
  if(!candidate)await page.unroute('**/__rum__/v1/**');
  checks.push('recovery succeeds while browser telemetry export is blocked');
  await page.locator('#rum-reset').click();
  await setMode(prior.control.mode,prior.control.delay_ms);prior=null;
  // A separate page makes the original page hidden, finalizing Web Vitals in Chromium.
  const other=await ctx.newPage();await other.goto('about:blank');
  await page.waitForTimeout(500);await flush();
  await page.bringToFront();await page.waitForTimeout(250);
  const observed=await page.evaluate(()=>window.DemoStoreRUM.status());
  report.page_id=observed.page_id;
  report.capabilities=observed.capabilities;
  assert(observed.capabilities.secure_context);
  const names=new Set(spans().map(s=>s.name));
  for(const name of ['browser.navigation','browser.resource','browser.long_task','browser.web_vital','browser.error','browser.checkout'])assert(names.has(name),name);
  // Standard fields read by generic RUM viewers (Zabbix APM "Real user monitoring").
  for(const name of ['documentLoad','documentFetch','resourceFetch'])assert(names.has(name),name);
  const sessionIds=new Set(spans().map(s=>decodeAttributes(s.attributes)['session.id']));
  assert(sessionIds.size===1&&[...sessionIds][0]&&/^[0-9a-f]{32}$/.test([...sessionIds][0]),'one session.id for the whole visit');
  assert(spans().every(s=>/^[0-9a-f]{24}$/.test(decodeAttributes(s.attributes)['view.id']||'')),'every span has a view.id');
  const load=spans().find(s=>s.name==='documentLoad');
  assert((load.events||[]).some(ev=>ev.name==='loadEventEnd'),'documentLoad carries timing events');
  assert(spans().some(s=>decodeAttributes(s.attributes).event_type==='click'),'click actions carry event_type');
  assert(spans().some(s=>decodeAttributes(s.attributes)['url.full']),'url.full present');
  const vitalNames=new Set(spans().filter(s=>s.name==='browser.web_vital').map(s=>decodeAttributes(s.attributes)['web.vital.name']));
  assert(vitalNames.has('FCP')&&vitalNames.has('TTFB'));
  report.web_vitals=[...vitalNames];
  for(const name of ['LCP','INP','CLS','FCP','TTFB'])assert(vitalNames.has(name),name);
  const vitalValues=spans().filter(s=>s.name==='browser.web_vital').map(s=>decodeAttributes(s.attributes));
  assert(vitalValues.some(a=>a['web.vital.name']==='INP'&&Number(a['web.vital.value'])>=500));
  assert(vitalValues.some(a=>a['web.vital.name']==='CLS'&&Number(a['web.vital.value'])>0));
  assert(vitalValues.some(a=>a['lab.browser_simulation']===true));
  checks.push('navigation/resources/long tasks and browser-supported Web Vitals exported');
  const payload=JSON.stringify(exports);
  for(const secret of ['demo_pass','demo_user','csrf-token','password','exception.stacktrace','exception.message'])assert(!payload.includes(secret),secret);
  const metricNames=exports.flatMap(e=>(e.body.resourceMetrics||[]).flatMap(r=>(r.scopeMetrics||[]).flatMap(s=>(s.metrics||[]).map(m=>m.name))));
  assert(metricNames.includes('demostore.browser.http.duration'));
  for(const batch of exports)for(const resource of batch.body.resourceMetrics||[])for(const scope of resource.scopeMetrics||[])for(const metric of scope.metrics||[]){
    assert.equal((metric.histogram||metric.sum).aggregationTemporality,1,'Browser metrics must use delta temporality');
  }
  assert(exports.some(e=>e.body.resourceLogs));
  report.metric_names=[...new Set(metricNames)];
  report.signals=[...new Set(exports.map(e=>e.path.split('/').pop()))];
  assert.equal(report.signals.length,3);
  checks.push('three OTLP signals and no credentials, input values or raw exception details in export');
  // Check manual labeling in a second, non-WebDriver-inferred manual test visit.
  // navigator.webdriver is normally true under Playwright; override only this test's classification signal.
  const manual=await browser.newContext();await manual.addInitScript("Object.defineProperty(navigator,'webdriver',{get:()=>false})");
  const manualPage=await manual.newPage();
  if(!candidate){
    await manualPage.goto(base+'/login');await manualPage.waitForFunction(()=>window.DemoStoreRUM);
    assert.equal(await manualPage.evaluate(()=>window.DemoStoreRUM.status().traffic_type),'manual');
    checks.push('manual-entry label with automation signal overridden only in the isolated test');
  }
  await manual.close();
  await page.locator('#logout-button').click();await page.waitForSelector('#login-form');
  checks.push('logout still works');
  assert(pageErrors.every(e=>e==='Error'));
  report.browser=browser.version();report.requests=requests;report.export_batches=exports.length;
  report.accepted_batches=exports.filter(e=>e.http_status===200).length;
  report.passed=true;
} finally {
  if(prior&&page)try{await setMode(prior.control.mode,prior.control.delay_ms);}catch{}
  if(process.env.RUM_REPORT_PATH)await writeFile(process.env.RUM_REPORT_PATH,JSON.stringify(report,null,2));
  if(process.env.RUM_EXPORT_PATH)await writeFile(process.env.RUM_EXPORT_PATH,JSON.stringify(exports,null,2));
  await browser.close();
}
console.log(JSON.stringify(report,null,2));
