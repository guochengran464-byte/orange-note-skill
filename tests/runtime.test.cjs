const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/../scripts/jupyter_runtime.js', 'utf8');

function page(sharedStorage = new Map()) {
  const storage = new Map();
  const events = {pending: 0, maxPending: 0, deletes: 0, creates: 0};
  const adapter = map => ({getItem:key=>map.get(key) ?? null, setItem:(key,val)=>map.set(key,val), removeItem:key=>map.delete(key)});
  class Socket {
    constructor() { setImmediate(() => this.onopen()); }
    send(text) {
      const message = JSON.parse(text);
      events.pending++;
      events.maxPending = Math.max(events.maxPending, events.pending);
      setTimeout(() => {
        events.pending--;
        this.onmessage({data: JSON.stringify({parent_header:{msg_id:message.header.msg_id},header:{msg_type:'status'},content:{execution_state:'idle'}})});
      }, message.content.code === 'slow' ? 40 : 1);
    }
    close() {}
  }
  const context = vm.createContext({
    window:{}, location:{pathname:'/notebook/st/test/lab', origin:'https://inner.dcs.cloud', protocol:'https:',host:'inner.dcs.cloud',search:''},
    document:{cookie:''}, URLSearchParams, WebSocket:Socket, setTimeout, clearTimeout,
    localStorage:adapter(sharedStorage), sessionStorage:adapter(storage),
    fetch:async (url, opts={}) => {
      if (opts.method === 'POST') events.creates++;
      if (opts.method === 'DELETE') events.deletes++;
      return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({id:'owned-' + events.creates})};
    }
  });
  vm.runInContext(source, context);
  return {context, events, run:(code, timeout_ms=1000)=>context.window.__codexJupyter('run_python',{code,timeout_ms})};
}

test('reinjecting runtime preserves one execution queue and does not delete another request', async () => {
  const p = page();
  const first = p.run('slow');
  await new Promise(resolve=>setTimeout(resolve, 5));
  vm.runInContext(source, p.context);
  const second = p.run('fast', 20);
  const results = await Promise.all([first, second]);
  assert.equal(p.events.maxPending, 1);
  assert.equal(p.events.deletes, 0);
  assert.ok(results.every(result=>result.ok));
  assert.equal(p.events.creates, 1);
});

test('separate controller pages do not share kernel ownership or cleanup', async () => {
  const shared = new Map();
  const a = page(shared), b = page(shared);
  await a.run('fast');
  await b.run('fast');
  assert.equal(a.events.creates, 1);
  assert.equal(b.events.creates, 1);
  await b.context.window.__codexJupyter('cleanup', {});
  assert.equal(a.events.deletes, 0);
});

test('owned kernel survives runtime reinjection and is cleaned up explicitly', async () => {
  const p = page();
  await p.run('fast');
  vm.runInContext(source, p.context);
  const next = await p.run('fast');
  assert.equal(next.data.kernel, 'reused');
  assert.equal(p.events.creates, 1);
  await p.context.window.__codexJupyter('cleanup', {});
  assert.equal(p.events.deletes, 1);
});
