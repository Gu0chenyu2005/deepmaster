/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

async function main() {
  const timers = [];
  let pendingOptions;
  const ctx = {
    window: {},
    sessionStorage: { getItem: () => 'token', setItem() {}, removeItem() {} },
    AbortController,
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; },
    fetch(url, options) {
      pendingOptions = options;
      if (url === '/api/run/t2') {
        return Promise.resolve({
          ok: true,
          headers: { get: () => 'application/json' },
          json: async () => ({ data: { ok: true } })
        });
      }
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        });
      });
    }
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('assets/js/api.js', 'utf8'), ctx);

  const result = ctx.window.dmApi.health();
  assert.equal(timers[0].ms, 20000);
  timers[0].fn();
  await assert.rejects(result, error => error.code === 408 && /超时/.test(error.message));
  assert(pendingOptions.signal.aborted);
  assert(timers[0].cleared);

  const action = await ctx.window.dmApi.runAction('t2', {});
  assert.equal(action.ok, true);
  assert.equal(timers[1].ms, 150000, 'run actions must outlive the backend proxy timeout');
  assert(timers[1].cleared);

  vm.runInContext(fs.readFileSync('assets/js/bridge.js', 'utf8'), ctx);
  const signaling = ctx.window.dmBridge.webrtcOffer({ session_id: 's', sdp: 'offer' });
  // 预算链：采集端 22s < agent 25s < 浏览器 35s
  assert.equal(timers[2].ms, 35000, 'signaling must outlive the agent proxy timeout');
  timers[2].fn();
  await assert.rejects(signaling, error => error.code === 408 && /超时/.test(error.message));

  const control = ctx.window.dmBridge.t1({}, null);
  assert.equal(timers[3].ms, 150000);
  timers[3].fn();
  const controlResult = await control;
  assert.equal(controlResult.ok, false);
  assert.match(controlResult.error, /超时/);

  console.log('API request timeout and error mapping passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
