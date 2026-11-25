// The page scripts must load without throwing (a constant used before its line,
// a typo at the top level...): node --check only reads the syntax. Runs map.js
// and app.js in a fake browser where every DOM call returns something harmless.
const fs = require('fs'), vm = require('vm'), path = require('path');
const any = () => new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'length' ? 0 : k === Symbol.iterator ? function* () {} : any()),
  apply: () => any(), construct: () => any(), set: () => true,
});
const ctx = { console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, requestAnimationFrame: () => 0,
  performance: { now: () => 0 }, Math, JSON, Date, Promise, Map, Set, Float32Array, Uint8Array, Array, Object, Number, String,
  parseInt, parseFloat, isFinite, encodeURIComponent, fetch: () => new Promise(() => {}), EventSource: function () { return any(); },
  localStorage: { getItem: () => null, setItem: () => {} }, matchMedia: () => any(), navigator: any(), location: any(),
  getComputedStyle: () => any(), document: any(), Image: function () { return any(); }, devicePixelRatio: 1,
  addEventListener: () => {}, alert: () => {}, confirm: () => false, prompt: () => null };
ctx.window = ctx;
vm.createContext(ctx);
for (const f of ['map.js', 'app.js', 'home.js', 'sensors.js']) {
  try { vm.runInContext(fs.readFileSync(path.join(__dirname, 'web', f), 'utf8'), ctx, { filename: f }); }
  catch (e) {
    if (e instanceof ReferenceError || e instanceof SyntaxError || /before initialization/.test(e.message)) {
      console.error(`${f} fails to load: ${e.message}`); process.exit(1);
    }
  }
}
if (typeof ctx.AckMap !== 'object') { console.error('map.js loaded but set no AckMap'); process.exit(1); }
if (typeof ctx.Overview !== 'object') { console.error('home.js loaded but set no Overview'); process.exit(1); }
if (typeof ctx.Sensors !== 'object') { console.error('sensors.js loaded but set no Sensors'); process.exit(1); }
// With a payload from the dashboard (test_multi.py passes one): Home renders it without an error
if (process.argv[2]) {
  ctx.__slow = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  try { vm.runInContext('slow = __slow; Overview.render(); Sensors.logPage(slow);', ctx); }
  catch (e) { console.error(`home.js fails to render: ${e.stack}`); process.exit(1); }
}
console.log('PAGES LOAD TESTS PASSED');
