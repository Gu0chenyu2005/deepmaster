/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync('assets/js/app.js', 'utf8');
const start = source.indexOf('  function dmPageTurn');
const end = source.indexOf('\n  var dmWelcome', start);
const timers = [];
const context = {
  setTimeout: fn => { timers.push(fn); return timers.length; }
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const classes = new Set();
const listeners = {};
let completed = false;
const element = {
  classList: {
    add: name => classes.add(name),
    remove: name => classes.delete(name)
  },
  get offsetWidth() { return 1; },
  addEventListener: (name, fn) => { listeners[name] = fn; },
  removeEventListener: name => { delete listeners[name]; }
};

context.dmPageTurn(element, 'is-pageturn-in', () => { completed = true; });
listeners.animationend({ target: {} });
assert(classes.has('is-pageturn-in'), 'child animation must not end the page turn');
assert(!completed, 'child animation must not settle the transition');

listeners.animationend({ target: element });
assert(!classes.has('is-pageturn-in'), 'page animation end must clear the transition class');
assert(completed, 'page animation end must settle the transition');
console.log('Page-turn cleanup ignores bubbled child animation events.');
