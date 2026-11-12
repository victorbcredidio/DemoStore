// A finite vocabulary prevents URL/input/DOM text capture and metric cardinality growth.
export const API_ROUTES = new Set([
  '/api/login', '/api/logout', '/api/session', '/api/products', '/api/cart',
  '/api/cart/items', '/api/cart/clear', '/api/checkout', '/api/lab/control', '/api/lab/reset'
]);
export function apiRoute(input, origin) {
  try {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, origin);
    return url.origin === origin && API_ROUTES.has(url.pathname) ? url.pathname : null;
  } catch { return null; }
}
export function pageRoute(path) { return path === '/login' ? '/login' : path === '/' ? '/' : 'other'; }
export function trafficType(value) { return ['manual', 'synthetic', 'unclassified'].includes(value) ? value : 'unclassified'; }
export function initialTraffic(path, meta, automated, origin) {
  if (new URL(path, origin).searchParams.get('traffic') === 'synthetic' || automated) return 'synthetic';
  return trafficType(meta);
}
export function resourceRoute(name, origin) {
  try {
    const url = new URL(name, origin);
    if (url.origin !== origin) return null;
    return ['/static/store.js', '/static/store.css', '/__rum__/rum-v1.js'].includes(url.pathname) ? url.pathname : null;
  } catch { return null; }
}
export function traceparent(context) {
  return `00-${context.traceId}-${context.spanId}-${(context.traceFlags & 1) ? '01' : '00'}`;
}
export function outcome(status) { return status >= 500 ? 'error' : status >= 400 ? 'rejected' : 'success'; }
// The page publishes DEMO_RELEASE_ID; accept only a short, plain token so it can't carry page data.
export function releaseVersion(value) {
  return typeof value === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(value) ? value : 'unknown';
}
// A visit: a random id kept in the tab's sessionStorage and renewed after 30 minutes without a page load.
// It is not persistent, not shared between tabs and says nothing about who the person is.
export const SESSION_IDLE_MS = 30 * 60 * 1000;
export function nextSession(saved, now, makeId) {
  const valid = saved && typeof saved.id === 'string' && /^[0-9a-f]{32}$/.test(saved.id)
    && Number.isFinite(saved.last) && now - saved.last >= 0 && now - saved.last < SESSION_IDLE_MS;
  return {id: valid ? saved.id : makeId(), last: now};
}
// Page address without query string or fragment, for the standard url.full attribute. Unknown paths stay 'other'.
export function pageUrl(origin, route) { return origin + (route === 'other' ? '/other' : route); }
