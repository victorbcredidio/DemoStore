import test from 'node:test';
import assert from 'node:assert/strict';
import {apiRoute, resourceRoute, initialTraffic, trafficType, pageRoute, traceparent, outcome, releaseVersion} from './policy.mjs';
const origin = 'https://store.example';
test('propagation is limited to exact same-origin app APIs', () => {
  assert.equal(apiRoute('/api/checkout?token=secret', origin), '/api/checkout');
  for (const path of ['https://elsewhere.test/api/checkout', 'http://store.example/api/checkout',
    '/__rum__/v1/traces', '/__rum__/v1/metrics', '/__rum__/v1/logs', '/api/unknown', '/static/store.js']) {
    assert.equal(apiRoute(path, origin), null);
  }
  assert.equal(apiRoute(new Request(origin + '/api/cart'), origin), '/api/cart');
});
test('resources exclude external and arbitrary URLs and remove queries', () => {
  assert.equal(resourceRoute(origin + '/static/store.js?secret=never-export', origin), '/static/store.js');
  assert.equal(resourceRoute('https://other.test/static/store.js', origin), null);
  assert.equal(resourceRoute(origin + '/private/user-name.png', origin), null);
});
test('classification has an explicit unknown state and recognizes synthetic entry', () => {
  assert.equal(trafficType('human-guess'), 'unclassified');
  assert.equal(initialTraffic('/login?traffic=synthetic', 'manual', false, origin), 'synthetic');
  assert.equal(initialTraffic('/login', 'manual', true, origin), 'synthetic');
  assert.equal(initialTraffic('/login', 'manual', false, origin), 'manual');
  assert.equal(initialTraffic('/login', null, false, origin), 'unclassified');
});
test('finite route, HTTP outcome and W3C trace context vocabulary', () => {
  assert.equal(pageRoute('/account/private'), 'other');
  assert.equal(outcome(200), 'success'); assert.equal(outcome(402), 'rejected'); assert.equal(outcome(500), 'error');
  assert.equal(traceparent({traceId:'a'.repeat(32), spanId:'b'.repeat(16), traceFlags:1}), '00-'+'a'.repeat(32)+'-'+'b'.repeat(16)+'-01');
});
test('service.version comes from the page release id and rejects anything else', () => {
  assert.equal(releaseVersion('02af451c9e1b'), '02af451c9e1b');
  assert.equal(releaseVersion('20261104-b8204721836d'), '20261104-b8204721836d');
  for (const value of [undefined, null, '', 'a b', '<script>', 'x'.repeat(65)]) assert.equal(releaseVersion(value), 'unknown');
});
