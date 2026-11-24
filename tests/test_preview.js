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
  const posts = [];
  const ctx = {
    window: {}, URL, Date,
    sessionStorage: { getItem: () => '' },
    fetch: async (url, options) => {
      urls.push(url);
      if (options && options.method === 'POST') posts.push({ url, options });
      if (url === '/api/run/cam-start') {
        return { ok: true, json: async () => ({ data: { camera_token: 'camera-secret' } }) };
      }
      return { ok: true, json: async () => ({ data: { cams: {} } }) };
    }
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('assets/js/bridge.js', 'utf8'), ctx);
  const bridge = ctx.window.dmBridge;
  const cfg = { _agent: { url: 'http://collector:5300', reachable: true } };
  const started = await bridge.camStart(cfg, null, 'direct');
  assert.equal(started.camera_token, 'camera-secret');
  assert((await bridge.frame(cfg, 'third')).startsWith('http://collector:5300/api/cam/snap/third?'));
  assert((await bridge.frame(cfg, 'third')).includes('token=camera-secret'));
  assert(bridge.streamUrl(cfg, 'wrist').startsWith('http://collector:5300/api/cam/stream/wrist?'));
  assert(bridge.streamUrl(cfg, 'wrist').includes('token=camera-secret'));
  assert(bridge.streamUrl(cfg, 'wrist', 'remote').includes('profile=remote'));
  await bridge.camHealth(cfg);
  assert.equal(urls[1], 'http://collector:5300/api/cam/health?token=camera-secret');
  const replacement = { _agent: { url: 'http://192.168.8.42:5312', reachable: true } };
  assert((await bridge.frame(replacement, 'third')).startsWith('http://192.168.8.42:5312/api/cam/snap/third?'));
  assert(bridge.streamUrl(replacement, 'wrist').startsWith('http://192.168.8.42:5312/api/cam/stream/wrist?'));
  await bridge.camHealth(replacement);
  assert.equal(urls[2], 'http://192.168.8.42:5312/api/cam/health?token=camera-secret');
  assert.throws(() => bridge.frame({}, 'third'));
  await bridge.t2(cfg, null, true);
  assert.equal(JSON.parse(posts[1].options.body).control_only, true);
  await bridge.camStart(cfg, null, 'direct');
  assert.equal(JSON.parse(posts[2].options.body).mode, 'direct');
  await bridge.webrtcOffer({ session_id: 's1', sdp: 'offer' });
  assert.equal(posts[3].url, '/api/webrtc/offer');
  assert.equal(JSON.parse(posts[3].options.body).session_id, 's1');
  await bridge.webrtcClose('s1');
  assert.equal(posts[4].url, '/api/webrtc/close');

  const requests = [];
  const jobs = [];
  const shown = [];
  const scope = {
    running: true, CAM_KEYS: ['wrist', 'global', 'third'],
    captureMode: 'near',
    frameTimer: null, camHealthTimer: null, previewGeneration: 0,
    PREVIEW_INTERVAL_MS: 200,
    previewControllers: {}, previewUrls: {},
    REMOTE_PROFILES: ['remote', 'remote_low', 'remote_min'], remoteProfileIndex: 0,
    remoteFrameTimes: { wrist: [], global: [], third: [] },
    remoteSourceFps: { wrist: 0, global: 0, third: 0 },
    remoteReconnectAttempts: { wrist: 0, global: 0, third: 0 },
    remoteStreamStarted: { wrist: 0, global: 0, third: 0 },
    remoteReconnectTimers: {}, remoteWatchTimer: null, remoteAdaptTimer: null,
    controlReconnectTimer: null, closeControlPeer: () => {},
    remoteHealthySince: 0,
    controlLatencyTimer: null, controlLatencySamples: [], controlLatencyBusy: false,
    controlLatencyGeneration: 0,
    window: { dmBridge: bridge, AbortController }, AbortController,
    document: { hidden: false },
    URL: { createObjectURL: () => 'blob:frame', revokeObjectURL: () => {} },
    $: () => null, $$: () => [],
    setCamFrame: (cam, src) => shown.push([cam, src]), startCamWatchdog: () => {},
    clearRemoteRenderers: () => {},
    setTimeout: (fn, ms) => { jobs.push({ fn, ms }); return jobs.length; },
    clearTimeout: () => {}, clearInterval: () => {},
    fetch: (url, options) => new Promise(resolve => requests.push({ url, options, resolve }))
  };
  vm.createContext(scope);
  const source = fs.readFileSync('assets/js/capture.js', 'utf8');
  let metricNow = 100;
  const metricScope = {
    window: { performance: { now: () => metricNow } },
    document: { hidden: false }, Date,
    running: true, captureMode: 'control', previewGeneration: 5,
    CAM_KEYS: ['wrist', 'global', 'third'],
    $: () => null, setInterval: () => 1, clearInterval: () => {},
    controlMetrics: {}, controlHistory: [], controlColors: ['#1683ff', '#19a974', '#e89016'],
    controlNames: { wrist: '腕', global: '头', third: '第三' }
  };
  vm.createContext(metricScope);
  const metricStart = source.indexOf('  var controlMetricTimer');
  const metricEnd = source.indexOf('  function clearRemoteRenderers', metricStart);
  vm.runInContext(source.slice(metricStart, metricEnd), metricScope);
  let videoFrameCallback;
  let callbackId = 0;
  const metricVideo = {
    requestVideoFrameCallback: callback => { videoFrameCallback = callback; return ++callbackId; }
  };
  metricScope.watchControlFrames('wrist', metricVideo, 5);
  videoFrameCallback(100, { captureTime: 80, expectedDisplayTime: 102 });
  videoFrameCallback(133, { captureTime: 113, expectedDisplayTime: 135 });
  metricNow = 1100;
  metricScope.sampleControlMetrics();
  assert.deepEqual(Array.from(metricScope.controlHistory[0].wrist), [22, 33, 2],
    'metrics use camera capture time, displayed frame spacing, and displayed FPS');
  let fallbackFrameCallback;
  metricScope.previewGeneration = 6;
  metricScope.watchControlFrames('global', {
    requestVideoFrameCallback: callback => { fallbackFrameCallback = callback; return 1; }
  }, 6);
  fallbackFrameCallback(200, { expectedDisplayTime: 202 });
  fallbackFrameCallback(233, { expectedDisplayTime: 235 });
  assert.equal(metricScope.controlMetrics.global.captureTimeSeen, false,
    'missing capture timestamps must not be reported as end-to-end latency');
  assert.deepEqual(Array.from(metricScope.controlMetrics.global.intervals), [33],
    'frame interval remains measurable without capture timestamps');
  metricScope.watchControlFrames('third', {}, 6);
  assert.equal(metricScope.controlMetrics.third.unsupported, true,
    'unsupported browsers must be identified instead of silently reporting no data');

  let readinessNow = 1000;
  const readinessTimers = [];
  const readinessScope = {
    CAM_KEYS: ['wrist', 'global', 'third'],
    controlVideoEls: {}, running: true, previewGeneration: 9,
    Date: { now: () => readinessNow },
    setInterval: (fn, ms) => { const timer = { fn, ms, active: true }; readinessTimers.push(timer); return timer; },
    clearInterval: timer => { timer.active = false; }
  };
  vm.createContext(readinessScope);
  const readyStart = source.indexOf('  function waitForControlVideoFrames(');
  const readyEnd = source.indexOf('  function scheduleControlReconnect(', readyStart);
  vm.runInContext(source.slice(readyStart, readyEnd), readinessScope);
  const videoReady = readinessScope.waitForControlVideoFrames(9, 1000);
  readinessTimers[0].fn();
  readinessScope.controlVideoEls = Object.fromEntries(['wrist', 'global', 'third'].map(cam =>
    [cam, { readyState: 2, videoWidth: 640 }]));
  readinessTimers[0].fn();
  assert.equal(await videoReady, true, 'control startup waits until all three videos have decoded a frame');
  assert.equal(readinessTimers[0].active, false);

  const timeoutScope = Object.assign({}, readinessScope, {
    controlVideoEls: {}, previewGeneration: 10,
    controlPeer: { connectionState: 'connecting', iceConnectionState: 'checking' },
    setInterval: readinessScope.setInterval,
    clearInterval: readinessScope.clearInterval
  });
  vm.createContext(timeoutScope);
  vm.runInContext(source.slice(readyStart, readyEnd), timeoutScope);
  const videoTimeout = timeoutScope.waitForControlVideoFrames(10, 100);
  const timeoutTimer = readinessTimers[1];
  readinessNow += 101;
  timeoutTimer.fn();
  await assert.rejects(videoTimeout, /H\.264 视频超时（连接 connecting，ICE checking/);

  let releaseClose;
  let peerClosed = false;
  let closedSession = '';
  const closeScope = {
    controlPeer: { close: () => { peerClosed = true; } },
    controlPeerSession: 'session-1',
    window: { dmBridge: { webrtcClose: id => {
      closedSession = id;
      return new Promise(resolve => { releaseClose = resolve; });
    } } },
    clearControlVideos: () => {}
  };
  vm.createContext(closeScope);
  const closeStart = source.indexOf('  function closeControlPeer()');
  const closeEnd = source.indexOf('  function waitForIceGathering(', closeStart);
  vm.runInContext(source.slice(closeStart, closeEnd), closeScope);
  const closing = closeScope.closeControlPeer();
  assert(peerClosed, 'the local peer closes immediately');
  await Promise.resolve();
  assert.equal(closedSession, 'session-1');
  let closeSettled = false;
  closing.then(() => { closeSettled = true; });
  await Promise.resolve();
  assert(!closeSettled, 'reconnect must wait for remote session cleanup');
  releaseClose();
  await closing;

  const midScope = { CAM_KEYS: ['wrist', 'global', 'third'] };
  vm.createContext(midScope);
  const midStart = source.indexOf('  function parseVideoMids(');
  const midEnd = source.indexOf('  function clearControlVideos(', midStart);
  vm.runInContext(source.slice(midStart, midEnd), midScope);
  assert.deepEqual(JSON.parse(JSON.stringify(midScope.parseVideoMids(
    'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:1\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:2\r\n'))),
    { '0': 'wrist', '1': 'global', '2': 'third' });

  const trackMapScope = {};
  vm.createContext(trackMapScope);
  const trackMapStart = source.indexOf('  function controlCameraForTrack(');
  const trackMapEnd = source.indexOf('  function clearControlVideos(', trackMapStart);
  vm.runInContext(source.slice(trackMapStart, trackMapEnd), trackMapScope);
  const wristTransceiver = { mid: '2' };
  const cameraByTransceiver = new Map([[wristTransceiver, 'wrist']]);
  assert.equal(trackMapScope.controlCameraForTrack(wristTransceiver, cameraByTransceiver,
    { '2': 'third' }, 'global'), 'wrist', 'transceiver identity takes precedence over mids');
  assert.equal(trackMapScope.controlCameraForTrack({ mid: '1' }, cameraByTransceiver,
    { '1': 'global' }, 'third'), 'global', 'SDP mid is used as a fallback');
  assert.equal(trackMapScope.controlCameraForTrack({ mid: null }, cameraByTransceiver,
    {}, 'third'), 'third', 'track order is the final fallback');

  const trackScope = { MediaStream: class {
    constructor(tracks) { this.tracks = tracks; }
  } };
  vm.createContext(trackScope);
  const trackStart = source.indexOf('  function controlTrackStream(');
  const trackEnd = source.indexOf('  function clearControlVideos(', trackStart);
  vm.runInContext(source.slice(trackStart, trackEnd), trackScope);
  const wristTrack = { id: 'wrist' };
  const globalTrack = { id: 'global' };
  const wristStream = trackScope.controlTrackStream(wristTrack);
  const globalStream = trackScope.controlTrackStream(globalTrack);
  assert.deepEqual(wristStream.tracks, [wristTrack]);
  assert.deepEqual(globalStream.tracks, [globalTrack]);
  assert.notEqual(wristStream, globalStream, 'each camera must get an isolated media stream');
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

  const parserScope = {};
  vm.createContext(parserScope);
  const start = source.indexOf('  function createMjpegParser(');
  const end = source.indexOf('  function setRemoteStatus(', start);
  vm.runInContext(source.slice(start, end), parserScope);
  const multipart = text => Buffer.from('--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + text.length + '\r\n\r\n' + text + '\r\n');
  const bytes = multipart('jpeg');
  for (let split = 1; split < bytes.length; split++) {
    const parser = parserScope.createMjpegParser();
    const first = parser.push(new Uint8Array(bytes.subarray(0, split)));
    const second = parser.push(new Uint8Array(bytes.subarray(split)));
    assert(!first.invalid && !second.invalid);
    assert.equal(Buffer.from(second.frame || first.frame).toString(), 'jpeg', 'all chunk boundaries must work');
  }
  const byteParser = parserScope.createMjpegParser();
  let lastFrame;
  for (const value of Buffer.concat([multipart('first'), multipart('latest')])) {
    const result = byteParser.push(new Uint8Array([value]));
    assert(!result.invalid);
    if (result.frame) lastFrame = result.frame;
  }
  assert.equal(Buffer.from(lastFrame).toString(), 'latest', 'single-byte fragments must not lose parser state');
  const coalesced = new Uint8Array(Buffer.concat([multipart('old'), multipart('new')]));
  const latest = parserScope.createMjpegParser().push(coalesced);
  assert.equal(Buffer.from(latest.frame).toString(), 'new', 'coalesced frames must keep only the latest');
  assert.equal(latest.frame.buffer, coalesced.buffer, 'complete frames must avoid an extra payload copy');
  assert(parserScope.createMjpegParser().push(new Uint8Array(2049).fill(65)).invalid,
    'oversized unfinished headers must fail immediately');
  for (const length of ['0', '8388609', '4junk']) {
    assert(parserScope.createMjpegParser().push(new Uint8Array(Buffer.from(
      '--frame\r\nContent-Length: ' + length + '\r\n\r\n'))).invalid, 'invalid payload length must fail');
  }
  const withMetadata = parserScope.createMjpegParser().push(new Uint8Array(Buffer.from(
    '--frame\r\nContent-Length: 4\r\nX-Frame-Seq: 128\r\nX-Capture-Time: 52304000\r\n\r\njpeg\r\n')));
  assert.equal(withMetadata.seq, 128);
  assert.equal(withMetadata.capturedAt, 52304000);
  const delay = parserScope.createFrameDelayTracker();
  assert(!delay.late(500000, 100), 'independent host clocks must not count as latency');
  assert(!delay.late(500100, 1000), 'one delayed packet must not reconnect');
  assert(!delay.late(500200, 300), 'normal delivery must clear an isolated delay');
  assert(!delay.late(500300, 1300));
  assert(!delay.late(500400, 1900));
  assert(delay.late(500500, 2500), 'sustained additional buffering must trigger recovery');
  assert(!delay.late(1000, 2600), 'a restarted source clock must establish a fresh baseline');

  const remoteRequests = [];
  const intervals = [];
  const animations = new Map();
  const blobs = [];
  const revoked = [];
  let clockMs = 100000;
  let nextAnimation = 1;
  const classes = () => {
    const values = new Set();
    return { add: x => values.add(x), remove: x => values.delete(x), contains: x => values.has(x),
      toggle: (x, enabled) => enabled ? values.add(x) : values.delete(x) };
  };
  const images = {};
  const labels = {};
  for (const cam of ['wrist', 'global', 'third']) {
    let src = '';
    images[cam] = {
      hidden: true, writes: 0, onload: null, onerror: null,
      get src() { return src; }, set src(value) { src = value; this.writes++; },
      getAttribute: () => src, removeAttribute: () => { src = ''; },
      closest: () => null, parentElement: { querySelector: () => null }
    };
    labels[cam] = { textContent: '' };
  }
  const stage = { classList: classes() };
  const healthBox = { hidden: false, classList: classes() };
  const statusLabel = { textContent: '' };
  const remoteScope = {
    running: true, captureMode: 'remote', CAM_KEYS: ['wrist', 'global', 'third'],
    frameTimer: null, camHealthTimer: null, previewGeneration: 0,
    PREVIEW_INTERVAL_MS: 200, previewControllers: {}, previewUrls: {},
    REMOTE_PROFILES: ['remote', 'remote_low', 'remote_min'], remoteProfileIndex: 0,
    remoteFrameTimes: { wrist: [], global: [], third: [] },
    remoteSourceFps: { wrist: 0, global: 0, third: 0 },
    remoteReconnectAttempts: { wrist: 0, global: 0, third: 0 },
    remoteStreamStarted: { wrist: 0, global: 0, third: 0 },
    remoteReconnectTimers: {}, remoteWatchTimer: null, remoteAdaptTimer: null,
    remoteHealthySince: 0,
    window: {
      dmBridge: bridge, AbortController,
      requestAnimationFrame: callback => { const id = nextAnimation++; animations.set(id, callback); return id; },
      cancelAnimationFrame: id => animations.delete(id)
    }, AbortController,
    document: { hidden: false },
    URL: {
      createObjectURL: blob => { blobs.push(blob); return 'blob:' + blobs.length; },
      revokeObjectURL: url => revoked.push(url)
    },
    Blob, Date: { now: () => clockMs }, Uint8Array, Number, Math, String,
    $: selector => {
      if (selector === '#cap-stage') return stage;
      if (selector === '#cap-preview-health') return healthBox;
      if (selector === '#cap-preview-status') return statusLabel;
      if (selector.startsWith('#cap-img-')) return images[selector.slice(9)];
      const label = /^\[data-cam-fps="(\w+)"\]$/.exec(selector);
      if (label) return labels[label[1]];
      return null;
    }, $$: () => [],
    setCamFrame: () => {},
    setInterval: (fn, ms) => { const job = { fn, ms, active: true }; intervals.push(job); return job; },
    clearInterval: job => { if (job) job.active = false; },
    setTimeout: () => 1, clearTimeout: () => {},
    fetch: (url, options) => {
      remoteRequests.push({ url, options });
      return new Promise(() => {});
    }
  };
  vm.createContext(remoteScope);
  const previewStart = source.indexOf('  var PREVIEW_INTERVAL_MS =');
  const previewEnd = source.indexOf('  var recListEl', previewStart);
  vm.runInContext(source.slice(previewStart, previewEnd), remoteScope);
  remoteScope.fetchFramePreview(cfg);
  await flush();
  assert.equal(remoteRequests.length, 3, 'remote preview opens all three camera streams');
  assert(remoteRequests.every(r => r.url.includes('/api/cam/stream/') && r.url.includes('profile=remote')),
    'remote preview uses direct continuous camera streams');
  remoteScope.stopFramePreview();

  remoteScope.captureMode = 'control';
  remoteScope.fetchFramePreview(cfg);
  await flush();
  assert.equal(remoteRequests.length, 3, 'control-only mode uses one WebRTC session instead of three MJPEG streams');
  assert(remoteScope.previewGeneration >= 3);
  const generation = remoteScope.previewGeneration;
  remoteScope.showRemoteFrame('wrist', new Uint8Array(Buffer.from('first')), generation);
  const firstLoad = images.wrist.onload;
  remoteScope.showRemoteFrame('wrist', new Uint8Array(Buffer.from('superseded')), generation);
  remoteScope.showRemoteFrame('wrist', new Uint8Array(Buffer.from('latest')), generation);
  assert.equal(blobs.length, 1, 'only one image may be decoding per camera');
  assert.equal(remoteScope.remoteFrameTimes.wrist.length, 0, 'received data must not count as displayed FPS');
  firstLoad();
  assert.equal(remoteScope.remoteFrameTimes.wrist.length, 1);
  assert.equal(images.wrist.writes, 1, 'showing a decoded image must not assign its src twice');
  assert.equal(animations.size, 1, 'decoding backlog must wait for a display opportunity');
  clockMs += 16;
  const draw = [...animations.values()][0];
  animations.clear();
  draw();
  assert.equal(blobs.length, 2);
  assert.equal(await blobs[1].text(), 'latest', 'decode backlog must retain only the newest frame');
  images.wrist.onload();
  assert.equal(remoteScope.remoteFrameTimes.wrist.length, 2);
  assert(revoked.includes('blob:1'), 'previous images must be released after replacement loads');
  remoteScope.showRemoteFrame('global', new Uint8Array([1]), generation);
  images.global.onerror();
  assert.equal(remoteScope.remoteFrameTimes.global.length, 0, 'failed image decodes must not count as FPS');
  remoteScope.showRemoteFrame('third', new Uint8Array([2]), generation);
  const staleLoad = images.third.onload;
  remoteScope.stopFramePreview();
  const frameCount = remoteScope.remoteFrameTimes.third.length;
  staleLoad();
  remoteScope.showRemoteFrame('third', new Uint8Array([3]), generation);
  assert.equal(remoteScope.remoteFrameTimes.third.length, frameCount, 'stale generations must not display or count frames');
  assert.equal(images.third.src, '', 'late decode callbacks must not restore stopped images');

  remoteScope.fetchFramePreview(cfg);
  await flush();
  remoteScope.captureMode = 'remote';
  remoteScope.fetchFramePreview(cfg);
  await flush();
  remoteScope.remoteProfileIndex = 2;
  remoteScope.remoteSourceFps = { wrist: 60, global: 60, third: 60 };
  const fillRates = wrist => {
    for (const cam of ['wrist', 'global', 'third']) {
      const fps = cam === 'wrist' ? wrist : 60;
      remoteScope.remoteFrameTimes[cam] = Array.from({ length: fps + 1 }, (_, i) => clockMs - 1000 + i * 1000 / fps);
    }
  };
  const adapt = intervals.filter(job => job.active && job.ms === 2000).at(-1).fn;
  clockMs += 11000;
  fillRates(40);
  adapt();
  assert.equal(remoteScope.remoteProfileIndex, 2, 'already using the fastest profile must not restart streams');
  clockMs += 4000;
  fillRates(40);
  adapt();
  await flush();
  assert.equal(remoteScope.remoteProfileIndex, 2, 'sustained slow rates stay on the fastest profile');
  clockMs += 2000;
  fillRates(60);
  adapt();
  clockMs += 20000;
  fillRates(60);
  adapt();
  await flush();
  assert.equal(remoteScope.remoteProfileIndex, 1, 'stable full FPS may restore the next higher quality profile');
  assert(remoteRequests.slice(-3).every(r => r.url.includes('profile=remote_low')));

  const watch = intervals.filter(job => job.active && job.ms === 500).at(-1).fn;
  remoteScope.remoteReceivedAt = { wrist: clockMs - 3000, global: clockMs - 3000, third: clockMs - 3000 };
  const currentStreams = remoteRequests.slice(-3);
  watch();
  assert(currentStreams.every(request => request.options.signal.aborted),
    'watchdog must inspect every camera even when the first camera is offline');
  remoteScope.stopFramePreview();
  console.log('Near preview, MJPEG parsing, bounded decoding, high-FPS adaptation and reconnect tests passed.');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
