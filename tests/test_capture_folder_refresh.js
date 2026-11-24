/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

async function main() {
  const source = fs.readFileSync('assets/js/capture.js', 'utf8');
  const start = source.indexOf('  var capDir = null');
  const end = source.indexOf('  function setFolderMode(', start);
  const pending = [];
  let calls = 0;
  const card = { hidden: false };
  const scope = {
    captureMode: 'near',
    window: { dmApi: { dirs: () => {
      calls++;
      if (calls === 1) {
        return new Promise(resolve => pending.push(resolve));
      }
      return Promise.resolve({ dirs: [] });
    } } },
    curAgent: { data_root: '/data' },
    devProbed: true,
    activeSession: { id: 'capture' },
    foldersBusy: false,
    hasCurrent: () => true,
    setFolderMode: () => {},
    renderFolders: folders => { card.hidden = folders.length === 0; },
    lastFolders: [],
    paintUpStates: () => {},
    syncUploadBtn: () => {}
  };
  vm.createContext(scope);
  vm.runInContext(source.slice(start, end), scope);

  scope.loadFolders();
  scope.loadFolders();
  scope.activeSession = null;
  pending[0]({ dirs: [{ name: 'stale', episodes: 1, records: 1 }] });
  for (let i = 0; i < 8; i++) await Promise.resolve();

  assert.equal(calls, 2, 'a refresh requested during an in-flight request must run afterward');
  assert.equal(card.hidden, true, 'the refreshed empty listing must hide the folder panel');

  const controlCard = { hidden: false };
  const controlEmpty = { hidden: false };
  let controlCalls = 0;
  const controlScope = {
    window: { dmApi: { dirs: () => { controlCalls++; return Promise.resolve({ dirs: [] }); } } },
    captureMode: 'control',
    curAgent: { data_root: '/data' },
    devProbed: true,
    activeSession: null,
    foldersBusy: false,
    hasCurrent: () => true,
    $: selector => selector === '#cap-records-card' ? controlCard
      : selector === '#cap-empty' ? controlEmpty : null,
    setFolderMode: () => {},
    recordingBase: () => '/data',
    renderFolders: () => { controlCard.hidden = false; },
    lastFolders: [],
    paintUpStates: () => {},
    syncUploadBtn: () => {}
  };
  vm.createContext(controlScope);
  vm.runInContext(source.slice(start, end), controlScope);
  controlScope.loadFolders();
  assert.equal(controlCalls, 0, 'control-only mode must not request recording folders');
  assert.equal(controlCard.hidden, true);
  assert.equal(controlEmpty.hidden, true);

  const renderStart = source.indexOf('  function renderFolders(');
  const renderEnd = source.indexOf('  function confirmDeleteFolder(', renderStart);
  vm.runInContext(source.slice(renderStart, renderEnd), controlScope);
  controlScope.renderFolders([{ name: 'stale', path: '/data/stale', episodes: 1 }], '/data');
  assert.equal(controlCard.hidden, true, 'late folder responses must stay hidden in control-only mode');
  console.log('Folder panel follows the latest listing after stop');
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
