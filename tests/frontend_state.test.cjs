const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../src/local_workspace_service/static/app.js'), 'utf8');

function element() {
  return {
    children: [], textContent: '', value: '', disabled: false,
    classList: { toggle() {} },
    appendChild(child) { this.children.push(child); return child; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener() {},
  };
}

function setup(request) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, createElement: element, addEventListener() {} },
    console,
  });
  vm.runInContext(source, context);
  context.requestJson = request;
  const state = vm.runInContext('state', context);
  state.workspaces = [workspace('A'), workspace('B')];
  return { context, state, get, alerts: () => get('alerts').children.map((item) => item.textContent).join('\n') };
}

function workspace(id, extensions = ['.md', '.docx']) {
  return { workspace_id: id, name: id, root_path: `D:/${id}`, allowed_extensions: extensions };
}

function latest(id) {
  return { status: 'available', needs_rescan: false, result: {
    workspace_id: id, scanned_at: '2026-11-08T00:00:00Z',
    included: [], excluded: [], errors: [],
    summary: { included_count: 0, excluded_count: 0, error_count: 0, visited_entries: 0, traversal_limit_reached: false },
  } };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('late A response cannot replace selected B, and switching clears A immediately', async () => {
  const pending = new Map();
  const h = setup((url) => {
    const response = deferred(); pending.set(url, response); return response.promise;
  });
  h.state.latest = latest('A');
  const a = h.context.selectWorkspace('A');
  const b = h.context.selectWorkspace('B');
  assert.equal(h.state.latest, null);
  pending.get('/api/v1/workspaces/B').resolve(workspace('B'));
  pending.get('/api/v1/workspaces/B/scan-result').resolve(latest('B'));
  await b;
  pending.get('/api/v1/workspaces/A').resolve(workspace('A'));
  pending.get('/api/v1/workspaces/A/scan-result').resolve(latest('A'));
  await a;
  assert.equal(h.state.workspace.workspace_id, 'B');
  assert.equal(h.state.latest.result.workspace_id, 'B');
  assert.equal(h.get('workspace-name').textContent, 'B');
});

test('refresh failure preserves old view and never announces successful refresh', async () => {
  const h = setup(async (url, options) => {
    if (options?.method === 'POST') return {};
    if (url.endsWith('/scan-result')) throw new Error('offline');
    return workspace('A', ['.md']);
  });
  h.state.workspace = workspace('A');
  const old = latest('A'); h.state.latest = old;
  await h.context.rescan();
  assert.equal(h.state.latest, old);
  assert.match(h.alerts(), /扫描已完成，但新清单加载失败/);
  assert.doesNotMatch(h.alerts(), /文件清单已保存并更新/);
  assert.equal(h.get('scan-button').disabled, false);
});

test('successful rescan refreshes displayed policy as well as result', async () => {
  const h = setup(async (url, options) => options?.method === 'POST' ? {} :
    url.endsWith('/scan-result') ? latest('A') : workspace('A', ['.md']));
  h.state.workspace = workspace('A');
  await h.context.rescan();
  assert.equal(h.get('workspace-extensions').textContent, '.md');
  assert.equal(h.state.workspaces[0].allowed_extensions.length, 1);
  assert.match(h.alerts(), /文件清单已保存并更新/);
});

test('scan finishing in background cannot update another workspace or enable its scan button', async () => {
  const scans = { A: deferred(), B: deferred() };
  const urls = [];
  const h = setup(async (url, options) => {
    urls.push(url);
    const id = url.split('/')[4];
    if (options?.method === 'POST') return scans[id].promise;
    return url.endsWith('/scan-result') ? latest(id) : workspace(id);
  });
  h.state.workspace = workspace('A');
  const a = h.context.rescan();
  await h.context.selectWorkspace('B');
  const b = h.context.rescan();
  await h.context.rescan(); // A second click must not submit another B scan.
  const before = urls.length;
  scans.A.resolve({}); await a;
  assert.equal(urls.length, before);
  assert.equal(h.state.latest.result.workspace_id, 'B');
  assert.equal(h.get('scan-button').disabled, true);
  assert.doesNotMatch(h.alerts(), /文件清单已保存并更新/);
  scans.B.resolve({}); await b;
  assert.equal(urls.filter((url) => url.endsWith('/B/scan')).length, 1);
  assert.equal(h.get('scan-button').disabled, false);
});

test('failed first load is unknown, not never scanned', async () => {
  const h = setup(async () => { throw new Error('offline'); });
  await h.context.selectWorkspace('A');
  assert.match(h.get('update-status').textContent, /未知/);
  assert.match(h.alerts(), /无法加载/);
  assert.doesNotMatch(h.alerts(), /该工作区尚未扫描/);
});

test('failed scan preserves old result and does not refresh it', async () => {
  const h = setup(async () => { throw new Error('root unavailable'); });
  h.state.workspace = workspace('A');
  const old = latest('A'); h.state.latest = old;
  await h.context.rescan();
  assert.equal(h.state.latest, old);
  assert.match(h.alerts(), /重新扫描失败/);
  assert.doesNotMatch(h.alerts(), /文件清单已保存并更新/);
});

test('older load for the same workspace cannot overwrite a newer load', async () => {
  const responses = [];
  const h = setup(() => { const d = deferred(); responses.push(d); return d.promise; });
  const first = h.context.selectWorkspace('A');
  const second = h.context.loadLatest('A');
  responses[2].resolve(workspace('A', ['.md']));
  responses[3].resolve(latest('A'));
  await second;
  responses[0].resolve(workspace('A'));
  responses[1].resolve(latest('A'));
  await first;
  assert.equal(h.get('workspace-extensions').textContent, '.md');
});

test('path-only registration uses defaults, selects the result and does not scan', async () => {
  const requests = [];
  const created = workspace('C');
  const h = setup(async (url, options) => {
    requests.push({ url, options });
    if (options?.method === 'POST') return created;
    return url.endsWith('/scan-result') ? { status: 'never_scanned', result: null } : created;
  });
  h.state.workspaces = [];
  h.get('workspace-root-input').value = '  "D:\\资料\\项目计划"  ';
  await h.context.createWorkspace();
  assert.deepEqual(JSON.parse(requests[0].options.body), { name: '项目计划', root_path: 'D:\\资料\\项目计划' });
  assert.equal(h.state.workspace.workspace_id, 'C');
  assert.equal(h.get('workspace-select').value, 'C');
  assert.equal(h.state.workspaces.length, 1);
  assert.equal(requests.filter((r) => r.options?.method === 'POST').length, 1);
  assert.equal(requests.some((r) => r.url.endsWith('/scan')), false);
  assert.match(h.get('workspace-create-message').textContent, /已创建并选中/);
});

test('registration failure keeps the input and previous selection', async () => {
  const h = setup(async () => { throw Object.assign(new Error('invalid'), { status: 422 }); });
  h.state.workspace = workspace('A');
  h.get('workspace-root-input').value = 'D:\\missing';
  h.get('workspace-name-input').value = '自定义名称';
  await h.context.createWorkspace();
  assert.equal(h.state.workspace.workspace_id, 'A');
  assert.equal(h.get('workspace-root-input').value, 'D:\\missing');
  assert.equal(h.get('workspace-name-input').value, '自定义名称');
  assert.equal(h.get('create-workspace-button').disabled, false);
  assert.match(h.get('workspace-create-message').textContent, /无法注册/);
});

test('duplicate registration explains how to use the existing workspace', async () => {
  const h = setup(async () => { throw Object.assign(new Error('duplicate'), { status: 409 }); });
  h.get('workspace-root-input').value = 'D:\\A';
  await h.context.createWorkspace();
  assert.equal(h.state.workspaces.length, 2);
  assert.match(h.get('workspace-create-message').textContent, /已注册/);
});

test('repeated registration submission is blocked and an optional name is preserved', async () => {
  const pending = deferred();
  let writes = 0;
  const h = setup(async (url, options) => {
    if (options?.method === 'POST') {
      writes++;
      assert.equal(JSON.parse(options.body).name, '自定义名称');
      return pending.promise;
    }
    return url.endsWith('/scan-result') ? latest('C') : workspace('C');
  });
  h.get('workspace-root-input').value = 'D:\\C';
  h.get('workspace-name-input').value = ' 自定义名称 ';
  const first = h.context.createWorkspace();
  await h.context.createWorkspace();
  assert.equal(writes, 1);
  pending.resolve(workspace('C'));
  await first;
});

test('old initial list cannot hide a newly registered workspace', async () => {
  const pending = deferred();
  const h = setup(async (url, options) => {
    if (url === '/api/v1/workspaces' && !options) return pending.promise;
    if (options?.method === 'POST') return workspace('C');
    return url.endsWith('/scan-result') ? latest('C') : workspace('C');
  });
  const initial = h.context.loadWorkspaces();
  h.get('workspace-root-input').value = 'D:\\C';
  await h.context.createWorkspace();
  pending.resolve([]);
  await initial;
  assert.equal(h.state.workspace.workspace_id, 'C');
  assert.equal(h.state.workspaces.some((item) => item.workspace_id === 'C'), true);
});

test('empty path does not send a request', async () => {
  const h = setup(async () => { assert.fail('unexpected request'); });
  h.get('workspace-root-input').value = '  ';
  await h.context.createWorkspace();
  assert.match(h.get('workspace-create-message').textContent, /请输入/);
});
