/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('assets/js/capture.js','utf8');
const block=source.slice(source.indexOf('  function fail('),source.indexOf('  function runSession('));
(async()=>{
 let resolve, calls=[];
 const cleanup=new Promise(r=>resolve=r);
 const ctx={running:true,timerStart:0,activeSession:{id:'S1'},sessionFolder:'run',cfgForRetry:{},
 stopTimer(){},stopFramePreview(){},setPhase(){},logLine(){},esc:s=>s,
 window:{dmApi:{endSession(...a){calls.push(a);return cleanup;}}},
 openModal(){return Promise.resolve(true)},hideLive(){},loadFolders(){},
 loadCfg(){return{}},CFG_DEFAULT:{},startCaptureSession(){calls.push('retry')},toast(){}};
 vm.createContext(ctx);vm.runInContext(block,ctx);ctx.fail('K7 failed','offline');
 await Promise.resolve();assert.equal(calls.length,1);assert.equal(calls[0][2],'error');
 resolve({empty_removed:true});for(let i=0;i<10;i++)await Promise.resolve();
 assert.equal(calls[1],'retry');assert.equal(ctx.activeSession,null);
 console.log('Failure cleanup completes before retry');
})().catch(e=>{console.error(e);process.exitCode=1});
