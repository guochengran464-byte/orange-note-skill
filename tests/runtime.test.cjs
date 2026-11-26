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

test('stat detects a Contents size mismatch using a bounded range response', async () => {
  const p = page();
  let cancelled = 0;
  p.context.fetch = async (url) => url.includes('api/contents/')
    ? {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({type:'file',size:10,last_modified:'link-time'})}
    : {ok:true,status:206,headers:{get:name=>name === 'content-range' ? 'bytes 0-0/524288' : '1'},body:{cancel:async()=>{cancelled++;}}};
  const result = await p.context.window.__codexJupyter('stat', {path:'work/link.bin'});
  assert.equal(result.ok, true);
  assert.equal(result.data.range_size, 524288);
  assert.equal(result.data.size, 10);
  assert.equal(cancelled, 1);
});

test('stat recognizes empty targets and cancels an ignored range without reading it', async () => {
  const p = page();
  let status = 416, cancelled = 0;
  p.context.fetch = async (url) => url.includes('api/contents/')
    ? {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({type:'file',size:9,last_modified:'link-time'})}
    : {ok:false,status,headers:{get:name=>name === 'content-range' ? 'bytes */0' : '0'},body:{cancel:async()=>{cancelled++;}}};
  assert.equal((await p.context.window.__codexJupyter('stat', {path:'work/link'})).data.range_size, 0);
  status = 200;
  assert.equal((await p.context.window.__codexJupyter('stat', {path:'work/link'})).data.range_size, null);
  assert.equal(cancelled, 2);
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

test('read_range reads tail bytes via HTTP Range without creating a kernel', async () => {
  const p = page();
  const full = Buffer.from('0123456789ABCDEF');
  p.context.TextDecoder = TextDecoder;
  p.context.fetch = async (url, opts = {}) => {
    if (url.includes('api/contents/')) {
      return {ok: true, status: 200, headers: {get: () => 'application/json'},
        json: async () => ({type: 'file', size: full.length, last_modified: 't'})};
    }
    const range = opts.headers && opts.headers.Range;
    if (range === 'bytes=0-0') {
      return {ok: true, status: 206, headers: {get: k => k === 'content-range' ? `bytes 0-0/${full.length}` : '1'},
        body: {cancel: async () => {}}};
    }
    const m = /^bytes=(\d+)-(\d+)$/.exec(range);
    const slice = full.subarray(Number(m[1]), Number(m[2]) + 1);
    return {ok: true, status: 206, headers: {get: () => String(slice.length)},
      arrayBuffer: async () => slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.byteLength)};
  };
  const tail = await p.context.window.__codexJupyter('read_range', {path: 'work/a.log', mode: 'tail', max_bytes: 6});
  assert.equal(tail.ok, true);
  assert.equal(tail.data.text, 'ABCDEF');
  assert.equal(tail.data.truncated, true);
  assert.equal(p.events.creates, 0);
});

test('batch_http executes multiple read requests in one evaluate call', async () => {
  const p = page();
  p.context.fetch = async (url) => {
    if (url.includes('content=0')) {
      return {ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({size: 5})};
    }
    const name = url.includes('a.json') ? '{"a":1}' : '{"b":2}';
    return {ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({content: name})};
  };
  const res = await p.context.window.__codexJupyter('batch_http', {
    requests: [
      {id: 'a', kind: 'read', path: 'work/a.json', max_bytes: 1024},
      {id: 'b', kind: 'read', path: 'work/b.json', max_bytes: 1024},
    ]
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.results.a.content, '{"a":1}');
  assert.equal(res.data.results.b.content, '{"b":2}');
  assert.equal(p.events.creates, 0);
});

test('put_chunk and rename_path call Jupyter Contents API', async () => {
  const p = page();
  const seen = [];
  p.context.fetch = async (url, opts = {}) => {
    seen.push({url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null});
    return {ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({size: 4})};
  };
  const r1 = await p.context.window.__codexJupyter('put_chunk', {path: 'work/a.tmp', b64: 'QUJDRA==', chunk: -1});
  const r2 = await p.context.window.__codexJupyter('rename_path', {old_path: 'work/a.tmp', new_path: 'work/a.bin'});
  assert.equal(r1.ok, true);
  assert.equal(r2.ok, true);
  assert.equal(seen[0].method, 'PUT');
  assert.equal(seen[0].body.chunk, -1);
  assert.equal(seen[1].method, 'PATCH');
  assert.equal(seen[1].body.path, 'work/a.bin');
});
