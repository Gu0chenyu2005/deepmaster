/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

async function main() {
  const source = fs.readFileSync('assets/js/review.js', 'utf8');
  const callbacks = {}, calls = [], scheduled = [], pending = [];
  const button = { disabled: false, textContent: '' };
  const status = { hidden: true, textContent: '', appendChild() {} };
  const ctx = {
    S: { dir: 'datasets/first' },
    $: id => id === '#rv-export' ? button : status,
    bindNav: (id, fn) => { callbacks[id] = fn; },
    clearTimeout() {}, setTimeout: fn => { scheduled.push(fn); return scheduled.length; },
    toast() {}, document: { createTextNode: text => text },
    api(path, opts) {
      calls.push({ path, opts });
      if (opts) { return Promise.resolve({ id: 'job' }); }
      return new Promise(resolve => pending.push(resolve));
    }
  };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('  var exportTimer'), source.indexOf('  var qcTimer')), ctx);
  const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); } };
  callbacks['rv-export']();
  await flush();
  assert.equal(calls[0].path, '/lerobot-export');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(calls[0].opts.body.dir, 'datasets/first');
  assert(calls.every(c => !c.path.includes('autocheck')));
  pending.shift()({ state: 'running', done: 1, total: 2, current: 'episode_4' });
  await flush();
  assert(button.disabled);
  assert.equal(button.textContent, '导出中 1/2');
  ctx.S.dir = 'datasets/second';
  ctx.syncExportState();
  pending.shift()(null);
  await flush();
  assert(!button.disabled);
  assert(status.hidden);
  scheduled.shift()();
  pending.shift()({ state: 'error', error: 'stale result' });
  await flush();
  assert(status.hidden, 'late results from another directory must not overwrite current UI');
  console.log('Independent export action, progress and directory isolation passed.');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
