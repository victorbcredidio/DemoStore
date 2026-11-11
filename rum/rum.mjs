import {trace, context, SpanKind, SpanStatusCode} from '@opentelemetry/api';
import {resourceFromAttributes} from '@opentelemetry/resources';
import {WebTracerProvider} from '@opentelemetry/sdk-trace-web';
import {BatchSpanProcessor} from '@opentelemetry/sdk-trace-base';
import {OTLPTraceExporter} from '@opentelemetry/exporter-trace-otlp-http';
import {MeterProvider, PeriodicExportingMetricReader} from '@opentelemetry/sdk-metrics';
import {OTLPMetricExporter, AggregationTemporalityPreference} from '@opentelemetry/exporter-metrics-otlp-http';
import {LoggerProvider, BatchLogRecordProcessor} from '@opentelemetry/sdk-logs';
import {OTLPLogExporter} from '@opentelemetry/exporter-logs-otlp-http';
import {onCLS, onFCP, onINP, onLCP, onTTFB} from 'web-vitals';
import {apiRoute, resourceRoute, pageRoute, trafficType, initialTraffic, traceparent, outcome, releaseVersion} from './policy.mjs';

// This observer intentionally does not install broad automatic DOM/fetch/error capture.
// Only the bounded fields constructed below can enter the SDKs.
if (!window.DemoStoreRUM) {
  try { start(); } catch { /* Observability must not prevent shopping. */ }
}
function start() {
  // service.version follows the backend release (DEMO_RELEASE_ID), published by the page.
  const version = releaseVersion(document.querySelector('meta[name="demostore-release"]')?.content);
  const origin = location.origin, route = pageRoute(location.pathname);
  const pageId = Array.from(crypto.getRandomValues(new Uint8Array(12)), b=>b.toString(16).padStart(2,'0')).join('');
  let traffic = initialTraffic(location.href, document.querySelector('meta[name="lab-traffic-type"]')?.content,
    navigator.webdriver === true, origin);
  const resource = resourceFromAttributes({'service.name':'demostore-browser', 'service.namespace':'user-experience-lab',
    'service.version':version, 'deployment.environment.name':'lab', 'telemetry.sdk.language':'webjs'});
  const traceProvider = new WebTracerProvider({resource, spanProcessors:[new BatchSpanProcessor(
    new OTLPTraceExporter({url:origin+'/__rum__/v1/traces', timeoutMillis:3000}),
    {maxQueueSize:256, maxExportBatchSize:32, scheduledDelayMillis:2000, exportTimeoutMillis:4000})]});
  const tracer = traceProvider.getTracer('demostore.rum',version);
  const metrics = new MeterProvider({resource, readers:[new PeriodicExportingMetricReader({
    exporter:new OTLPMetricExporter({url:origin+'/__rum__/v1/metrics',timeoutMillis:3000,
      temporalityPreference:AggregationTemporalityPreference.DELTA}),
    exportIntervalMillis:10000, exportTimeoutMillis:4000})]});
  const loggerProvider = new LoggerProvider({resource, processors:[new BatchLogRecordProcessor(
    new OTLPLogExporter({url:origin+'/__rum__/v1/logs',timeoutMillis:3000}),
    {maxQueueSize:128,maxExportBatchSize:16,scheduledDelayMillis:2000,exportTimeoutMillis:4000})]});
  const logger = loggerProvider.getLogger('demostore.rum',version), meter = metrics.getMeter('demostore.rum',version);
  const requestDuration = meter.createHistogram('demostore.browser.http.duration',{unit:'ms'});
  const actionDuration = meter.createHistogram('demostore.browser.checkout.duration',{unit:'ms'});
  const longDuration = meter.createHistogram('demostore.browser.long_task.duration',{unit:'ms'});
  const errorCount = meter.createCounter('demostore.browser.errors',{unit:'{error}'});
  const state = {version, page_id:pageId, observations:0, dropped:0, errors:0, requests:0, simulation_used:false, recent:[],
    capabilities:{secure_context:isSecureContext, performance_observer:typeof PerformanceObserver!=='undefined',
      entry_types:typeof PerformanceObserver!=='undefined' ? [...PerformanceObserver.supportedEntryTypes] : []}};
  const metricAttrs = (extra={}) => ({'page.route':route,'lab.traffic_type':traffic,
    'lab.browser_simulation':state.simulation_used,...extra});
  const attrs = (extra={}) => ({...metricAttrs(),'lab.synthetic':traffic==='synthetic','rum.page.id':pageId,...extra});
  const epoch = ms => performance.timeOrigin + ms;
  const allowed = () => {if(state.observations>=600){state.dropped++; return false;} state.observations++; return true;};
  function remember(name, data) {state.recent.push({name,...data}); if(state.recent.length>30)state.recent.shift();}
  function span(name, data={}, start=performance.now(), end=start, parent) {
    if(!allowed())return null;
    const s = tracer.startSpan(name,{attributes:attrs(data),startTime:epoch(start)},parent);
    if(name==='browser.error')s.setStatus({code:SpanStatusCode.ERROR});
    s.end(epoch(Math.max(start,end))); remember(name,data); return s;
  }
  function log(name, data, currentSpan, severity=9) {
    logger.emit({eventName:name,body:name.replaceAll('.',' '),severityNumber:severity,
      severityText:severity>=17?'ERROR':'INFO',attributes:attrs(data),
      context:currentSpan?trace.setSpan(context.active(),currentSpan):context.active()});
  }
  const flush = () => Promise.allSettled([traceProvider.forceFlush(),metrics.forceFlush(),loggerProvider.forceFlush()]);
  let checkout = null;
  function finishCheckout(result) {
    const item = checkout;
    if(!item)return;
    checkout=null; clearTimeout(item.timeout);
    const finish = () => {
      const duration = Math.max(0,performance.now()-item.start);
      item.span.setAttributes({'ui.outcome':result,'ui.duration_ms':duration});
      if(result==='error')item.span.setStatus({code:SpanStatusCode.ERROR});
      item.span.end();
      actionDuration.record(duration,metricAttrs({'ui.outcome':result}));
      log('browser.checkout.visible_result',{'ui.outcome':result,'ui.duration_ms':duration},item.span);
      remember('browser.checkout.visible_result',{'ui.outcome':result,'ui.duration_ms':duration});
    };
    // A rendering opportunity after the DOM update, not proof of physical display/read.
    if(document.visibilityState==='visible')requestAnimationFrame(()=>requestAnimationFrame(finish)); else finish();
  }
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function(input, init) {
    const path = apiRoute(input,origin);
    if(!path)return nativeFetch(input,init);
    let s, start, options;
    try {
      const method = String(init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
      if(!['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'].includes(method)||!allowed())return nativeFetch(input,init);
      start=performance.now();
      const parent = path==='/api/checkout' && checkout ? trace.setSpan(context.active(),checkout.span) : context.active();
      s=tracer.startSpan(method+' '+path,{kind:SpanKind.CLIENT,attributes:attrs({'http.request.method':method,'http.route':path}),startTime:epoch(start)},parent);
      const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('traceparent',traceparent(s.spanContext()));
      options={...init,headers};
    } catch {return nativeFetch(input,init);}
    const finish=(status,error)=>{
      const duration=performance.now()-start, result=error?'network_error':outcome(status);
      const data={'http.route':path,'http.response.status_code':status,'http.client.duration_ms':duration,'http.outcome':result};
      s.setAttributes(data); if(error||status>=500)s.setStatus({code:SpanStatusCode.ERROR}); s.end();
      requestDuration.record(duration,metricAttrs({'http.route':path,'http.outcome':result}));
      state.requests++; remember('browser.http',data);
    };
    const safeFinish=(status,error)=>{try{finish(status,error);}catch{state.dropped++;}};
    try {
      return nativeFetch(input,options).then(response=>{
        safeFinish(response.status,false);
        try {
          const reported=response.headers.get('X-Lab-Traffic-Type');
          if(reported)traffic=trafficType(reported);
        } catch {}
        return response;
      },error=>{safeFinish(0,true);throw error;});
    } catch(error) {safeFinish(0,true);throw error;}
  };
  function observe(type, callback) {
    if(!state.capabilities.entry_types.includes(type))return;
    try {new PerformanceObserver(list=>{for(const entry of list.getEntries())callback(entry);})
      .observe({type,buffered:true});} catch {}
  }
  observe('resource',e=>{
    const name=resourceRoute(e.name,origin); if(!name)return;
    span('browser.resource',{'resource.route':name,'resource.duration_ms':e.duration,
      'resource.transfer_bytes':e.transferSize},e.startTime,e.responseEnd);
  });
  observe('longtask',e=>{
    const s=span('browser.long_task',{'browser.long_task.duration_ms':e.duration},e.startTime,e.startTime+e.duration);
    if(s)longDuration.record(e.duration,metricAttrs());
  });
  const vitals = new Map();
  function vital(metric) {
    const revision=(vitals.get(metric.id)||0)+1; vitals.set(metric.id,revision);
    const data={'web.vital.name':metric.name,'web.vital.value':metric.value,'web.vital.rating':metric.rating,
      'web.vital.id':metric.id,'web.vital.sequence':revision,'web.vital.unit':metric.name==='CLS'?'1':'ms'};
    const s=span('browser.web_vital',data); if(s)log('browser.web_vital',data,s);
    // Do not histogram lifecycle revisions: population queries deduplicate by metric.id.
  }
  for(const register of [onCLS,onFCP,onINP,onLCP,onTTFB])try{register(vital,{reportAllChanges:true});}catch{}
  const navigation=()=>{
    const e=performance.getEntriesByType('navigation')[0]; if(!e)return;
    const end=e.loadEventEnd||e.domContentLoadedEventEnd||performance.now();
    const s=span('browser.navigation',{'navigation.duration_ms':end,'navigation.ttfb_ms':e.responseStart-e.startTime,
      'navigation.dom_interactive_ms':e.domInteractive,'navigation.dom_content_loaded_ms':e.domContentLoadedEventEnd,
      'navigation.type':['navigate','reload','back_forward','prerender'].includes(e.type)?e.type:'other'},0,end);
    if(s)log('browser.page.observed',{'browser.secure_context':isSecureContext},s);
  };
  if(document.readyState==='complete')navigation();else window.addEventListener('load',()=>setTimeout(navigation,0),{once:true});
  function browserError(type) {
    if(state.errors>=10)return;
    state.errors++;
    const data={'error.type':type}, s=span('browser.error',data);
    if(s){log('browser.error',data,s,17);errorCount.add(1,metricAttrs(data));}
  }
  window.addEventListener('error',e=>browserError(e instanceof ErrorEvent?'uncaught_exception':'resource_load_error'),true);
  window.addEventListener('unhandledrejection',()=>browserError('unhandled_rejection'));
  document.addEventListener('click',e=>{
    const button=e.target instanceof Element?e.target.closest('button'):null;
    if(!button||button.disabled)return;
    if(button.id==='checkout-button'&&!checkout&&allowed()){
      const start=performance.now(), s=tracer.startSpan('browser.checkout',{attributes:attrs({'ui.action':'checkout'}),startTime:epoch(start)});
      checkout={span:s,start,timeout:setTimeout(()=>finishCheckout('timeout'),30000)};
    } else if(['login-submit','logout-button','add-laptop','add-headphones','clear-cart-button'].includes(button.id)) {
      span('browser.action',{'ui.action':button.id});
    }
  },true);
  const result=document.getElementById('checkout-result');
  if(result)new MutationObserver(()=>{
    if(checkout&&['success','error'].includes(result.dataset.state))finishCheckout(result.dataset.state);
  }).observe(result,{attributes:true,attributeFilter:['data-state']});
  function simulate(mode) {
    if(mode!=='reset')state.simulation_used=true;
    span('browser.lab.simulation',{'lab.browser_simulation.kind':mode});
    if(mode==='stall') {const start=performance.now();while(performance.now()-start<600){} }
    if(mode==='shift')setTimeout(()=>{
      let banner=document.getElementById('rum-layout-demo');
      if(!banner){banner=document.createElement('p');banner.id='rum-layout-demo';banner.className='login-card';
        banner.textContent='Browser RUM lab: delayed banner inserted to demonstrate layout movement.';
        const anchor=document.documentElement.style.overflowAnchor;
        document.documentElement.style.overflowAnchor='none';
        document.querySelector('main')?.prepend(banner);
        setTimeout(()=>{document.documentElement.style.overflowAnchor=anchor;},1200);}
    },700);
    if(mode==='error')setTimeout(()=>{throw new Error('DemoStore browser lab error');},0);
    if(mode==='rejection')Promise.reject(new Error('DemoStore browser lab rejection'));
    if(mode==='reset')document.getElementById('rum-layout-demo')?.remove();
  }
  const controls=document.getElementById('lab-controls');
  if(controls){
    const panel=document.createElement('section'), title=document.createElement('h3'), info=document.createElement('p');
    const buttons=document.createElement('div');buttons.className='mode-buttons';
    panel.id='rum-lab';panel.className='lab-body';title.textContent='Browser RUM lab';
    info.textContent='One-shot simulations in this tab. Browser measurements use service demostore-browser.';
    panel.append(title,info);
    for(const [mode,label] of [['stall','Stall browser 600 ms'],['shift','Delayed layout shift'],['error','JavaScript error'],['rejection','Unhandled rejection'],['reset','Remove demo banner']]){
      const button=document.createElement('button');button.type='button';button.id='rum-'+mode;button.textContent=label;
      button.addEventListener('click',()=>simulate(mode));buttons.append(button);
    }
    panel.append(buttons);
    controls.append(panel);
  }
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')flush();});
  window.addEventListener('pagehide',()=>{if(checkout)finishCheckout('abandoned');flush();});
  window.DemoStoreRUM=Object.freeze({status:()=>({...state,traffic_type:traffic,recent:[...state.recent]}),flush});
}
