const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/../scripts/jupyter_runtime.js', 'utf8');

function page(sharedStorage = new Map(), scenario = 'success') {
  const storage = new Map();
  const events = {pending: 0, maxPending: 0, deletes: 0, creates: 0, opened: 0, closed: 0};
  const adapter = map => ({getItem:key=>map.get(key) ?? null, setItem:(key,val)=>map.set(key,val), removeItem:key=>map.delete(key)});
  class Socket {
    constructor() {
      events.opened++;
      setImmediate(() => {
        if (scenario === 'socket_error') this.onerror?.();
        else this.onopen?.();
      });
    }
    send(text) {
      const message = JSON.parse(text);
      events.pending++;
      events.maxPending = Math.max(events.maxPending, events.pending);
      if (scenario === 'timeout') return;
      if (scenario === 'early_close') { setImmediate(() => this.onclose?.()); return; }
      if (scenario === 'foreign_idle') {
        this.onmessage({data: JSON.stringify({parent_header: {}, header: {msg_type: 'status'}, content: {execution_state: 'idle'}})});
      }
      setTimeout(() => {
        events.pending--;
        if (scenario === 'python_error') this.onmessage?.({data: JSON.stringify({parent_header:{msg_id:message.header.msg_id},header:{msg_type:'error'},content:{ename:'ValueError',evalue:'fixture',traceback:[]}})});
        this.onmessage?.({data: JSON.stringify({parent_header:{msg_id:message.header.msg_id},header:{msg_type:'status'},content:{execution_state:'idle'}})});
      }, message.content.code === 'slow' ? 40 : 1);
    }
    close() { events.closed++; this.onclose?.(); }
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
  return {context, events, setScenario:value=>{scenario=value;}, run:(code, timeout_ms=1000)=>context.window.__codexJupyter('run_python',{code,timeout_ms})};
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

test('100 completed requests close their sockets and retain one usable kernel', async () => {
  const p = page();
  for (let i = 0; i < 100; i++) assert.equal((await p.run('fast')).ok, true);
  assert.equal(p.events.opened, 100);
  assert.equal(p.events.closed, 100);
  assert.equal(p.events.creates, 1);
  assert.equal(p.events.deletes, 0);
});

for (const scenario of ['timeout', 'socket_error', 'early_close', 'python_error']) {
  test(`${scenario} closes the channel and permits a later request`, async () => {
    const p = page(new Map(), scenario);
    const first = await p.run('fast', 15);
    assert.equal(first.ok, false);
    assert.equal(p.events.closed, 1);
    assert.equal(p.events.deletes, scenario === 'python_error' ? 0 : 1);
    p.setScenario('success');
    assert.equal((await p.run('fast')).ok, true);
    assert.equal(p.events.closed, 2);
    assert.equal(p.events.creates, scenario === 'python_error' ? 1 : 2);
  });
}

test('an unrelated idle event cannot finish a running request', async () => {
  const p = page(new Map(), 'foreign_idle');
  const result = await p.run('slow');
  assert.equal(result.ok, true);
  assert.equal(p.events.pending, 0);
  assert.equal(p.events.closed, 1);
});

test('an ignored Range is rejected before reading a large response body', async () => {
  const p = page();
  let reads = 0, cancelled = 0;
  p.context.fetch = async () => ({status: 200, headers: {get: () => String(4 * 1024 ** 3)},
    body: {cancel: async () => {cancelled++;}}, arrayBuffer: async () => {reads++; return new ArrayBuffer(0);}});
  const result = await p.context.window.__codexJupyter('fetch_range', {path: 'work/big.bin', start: 0, end: 262143});
  assert.equal(result.ok, false);
  assert.equal(result.error, 'range_not_supported');
  assert.equal(reads, 0);
  assert.equal(cancelled, 1);
});

test('partial responses and complete small files retain binary download support', async () => {
  const p = page();
  p.context.btoa = value => Buffer.from(value, 'binary').toString('base64');
  for (const status of [200, 206]) {
    p.context.fetch = async () => ({status, headers: {get: () => '3'},
      arrayBuffer: async () => Uint8Array.from([0, 128, 255]).buffer});
    const result = await p.context.window.__codexJupyter('fetch_range', {path: 'work/small.bin', start: 0, end: 2});
    assert.equal(result.ok, true);
    assert.equal(result.data.b64, Buffer.from([0, 128, 255]).toString('base64'));
  }
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
