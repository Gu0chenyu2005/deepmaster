/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

async function main() {
  const urls = [];
  const ctx = {
    window: {}, URL, Date,
    sessionStorage: { getItem: () => '' },
    fetch: async url => {
      urls.push(url);
      return { ok: true, json: async () => ({ data: { cams: {} } }) };
    }
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('assets/js/bridge.js', 'utf8'), ctx);
  const bridge = ctx.window.dmBridge;
  const cfg = { _agent: { url: 'http://collector:5300', reachable: true } };
  assert((await bridge.frame(cfg, 'third')).startsWith('http://collector:5300/api/cam/snap/third?'));
  assert(bridge.streamUrl(cfg, 'wrist').startsWith('http://collector:5300/api/cam/stream/wrist?'));
  await bridge.camHealth(cfg);
  assert.equal(urls[0], 'http://collector:5300/api/cam/health');
  const replacement = { _agent: { url: 'http://192.168.8.42:5312', reachable: true } };
  assert((await bridge.frame(replacement, 'third')).startsWith('http://192.168.8.42:5312/api/cam/snap/third?'));
  assert(bridge.streamUrl(replacement, 'wrist').startsWith('http://192.168.8.42:5312/api/cam/stream/wrist?'));
  await bridge.camHealth(replacement);
  assert.equal(urls[1], 'http://192.168.8.42:5312/api/cam/health');
  assert.throws(() => bridge.frame({}, 'third'));

  const requests = [];
  const jobs = [];
  const shown = [];
  const scope = {
    running: true, CAM_KEYS: ['wrist', 'global', 'third'],
    frameTimer: null, camHealthTimer: null, previewGeneration: 0,
    PREVIEW_INTERVAL_MS: 200,
    previewControllers: {}, previewUrls: {},
    window: { dmBridge: bridge, AbortController }, AbortController,
    document: { hidden: false },
    URL: { createObjectURL: () => 'blob:frame', revokeObjectURL: () => {} },
    setCamFrame: (cam, src) => shown.push([cam, src]), startCamWatchdog: () => {},
    setTimeout: (fn, ms) => { jobs.push({ fn, ms }); return jobs.length; },
    clearTimeout: () => {}, clearInterval: () => {},
    fetch: (url, options) => new Promise(resolve => requests.push({ url, options, resolve }))
  };
  vm.createContext(scope);
  const source = fs.readFileSync('assets/js/capture.js', 'utf8');
  vm.runInContext(source.slice(source.indexOf('  function fetchFramePreview(cfg)'), source.indexOf('  var recListEl')), scope);
  scope.fetchFramePreview(cfg);
  const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
  await flush();
  assert.equal(requests.length, 3);
  assert(requests.every(r => r.url.startsWith('http://collector:5300/api/cam/snap/')));
  assert.equal(jobs.filter(j => j.ms === 200).length, 0, 'slow frames must not queue new requests');
  requests[0].resolve({ ok: true, blob: async () => new Blob(['jpeg']) });
  await flush();
  assert.equal(jobs.filter(j => j.ms === 200).length, 1, 'preview is capped at about 5 FPS per camera');
  scope.stopFramePreview();
  assert(requests[1].options.signal.aborted);
  const before = shown.length;
  requests[1].resolve({ ok: true, blob: async () => new Blob(['stale']) });
  await flush();
  assert.equal(shown.length, before, 'stopped previews must ignore late responses');
  jobs.filter(j => j.ms === 200).forEach(j => j.fn());
  await flush();
  assert.equal(requests.length, 3, 'stale callbacks must not restart previews');
  console.log('Direct preview and slow-response cancellation tests passed.');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
