const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../src/local_workspace_service/static/model-connections.js'), 'utf8');

function element() {
  return {
    children: [], textContent: '', value: '', checked: false, disabled: false,
    className: '', dataset: {}, open: false,
    classList: { toggle() {} },
    appendChild(child) { this.children.push(child); return child; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener() {},
    showModal() { this.open = true; },
    close() { this.open = false; },
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
    console, URL, Object, JSON,
  });
  vm.runInContext(source, context);
  context.modelRequestJson = request;
  const state = vm.runInContext('connectionState', context);
  return { context, state, get };
}

function connection(id, extra = {}) {
  return {
    id, name: `配置 ${id}`, protocol: 'openai_responses',
    base_url: `https://${id}.example.test/v1`, model: `model-${id}`,
    has_api_key: true, is_active: false,
    created_at: '2026-11-08T00:00:00Z', updated_at: '2026-11-08T00:00:00Z',
    ...extra,
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function validForm(h) {
  h.get('connection-name').value = '测试配置';
  h.get('connection-protocol').value = 'openai_responses';
  h.get('connection-base-url').value = 'https://models.example.test/custom';
  h.get('connection-model').value = 'test-model';
  h.get('connection-api-key').value = 'fake-browser-key';
}

test('late edit response cannot overwrite a newer edit target', async () => {
  const pending = new Map();
  const h = setup((url) => { const d = deferred(); pending.set(url, d); return d.promise; });
  h.state.items = [connection('A'), connection('B')];
  const first = h.context.openEditor('A');
  const second = h.context.openEditor('B');
  pending.get('/api/v1/model-connections/B').resolve(connection('B'));
  await second;
  pending.get('/api/v1/model-connections/A').resolve(connection('A'));
  await first;
  assert.equal(h.state.editingId, 'B');
  assert.equal(h.get('connection-name').value, '配置 B');
});

test('duplicate save submission sends only one write request', async () => {
  const save = deferred();
  let writes = 0;
  const h = setup(async (url, options) => {
    if (options?.method === 'POST') { writes += 1; return save.promise; }
    return { items: [connection('A')], active_connection_id: null };
  });
  h.context.openCreateEditor();
  validForm(h);
  const first = h.context.saveConnection({ preventDefault() {} });
  const second = h.context.saveConnection({ preventDefault() {} });
  assert.equal(writes, 1);
  save.resolve(connection('A'));
  await Promise.all([first, second]);
  assert.equal(writes, 1);
});

test('save failure keeps retryable form values and does not show success', async () => {
  const h = setup(async () => { throw new Error('credential unavailable'); });
  h.context.openCreateEditor();
  validForm(h);
  await h.context.saveConnection({ preventDefault() {} });
  assert.equal(h.get('connection-name').value, '测试配置');
  assert.equal(h.get('connection-api-key').value, 'fake-browser-key');
  assert.match(h.get('form-error').textContent, /保存失败/);
  assert.doesNotMatch(h.get('page-message').textContent, /已保存/);
});

test('failed activation preserves original active selection', async () => {
  const h = setup(async () => { throw new Error('database unavailable'); });
  h.state.items = [connection('A', { is_active: true }), connection('B')];
  h.state.activeId = 'A';
  await h.context.activateConnection('B');
  assert.equal(h.state.activeId, 'A');
  assert.equal(h.state.items[0].is_active, true);
  assert.match(h.get('page-message').textContent, /切换当前配置失败/);
});

test('cancel delete changes no data and failed delete does not hide item', async () => {
  let deletes = 0;
  const h = setup(async (url, options) => {
    if (options?.method === 'DELETE') { deletes += 1; throw new Error('credential delete failed'); }
  });
  h.state.items = [connection('A', { is_active: true })];
  h.state.activeId = 'A';
  h.context.openDeleteDialog('A');
  h.context.cancelDelete();
  assert.equal(deletes, 0);
  assert.equal(h.state.items.length, 1);
  h.context.openDeleteDialog('A');
  await h.context.confirmDelete();
  assert.equal(deletes, 1);
  assert.equal(h.state.items.length, 1);
  assert.equal(h.state.activeId, 'A');
  assert.match(h.get('page-message').textContent, /删除失败/);
});

test('saving A cannot close or clear B editor when A response arrives late', async () => {
  const saveA = deferred();
  const loadB = deferred();
  const h = setup((url, options) => {
    if (url.endsWith('/A') && options?.method === 'PATCH') return saveA.promise;
    if (url.endsWith('/B')) return loadB.promise;
    return Promise.resolve({ items: [connection('A'), connection('B')], active_connection_id: null });
  });
  h.state.items = [connection('A'), connection('B')];
  h.state.editingId = 'A';
  validForm(h);
  const saving = h.context.saveConnection({ preventDefault() {} });
  const editing = h.context.openEditor('B');
  loadB.resolve(connection('B'));
  await editing;
  saveA.resolve(connection('A'));
  await saving;
  assert.equal(h.state.editingId, 'B');
  assert.equal(h.get('connection-name').value, '配置 B');
});

test('activation writes are serialized so server and page keep the final selection', async () => {
  const pendingA = deferred();
  const pendingB = deferred();
  const requests = [];
  const h = setup((url) => { requests.push(url); return url.includes('/A/') ? pendingA.promise : pendingB.promise; });
  h.state.items = [connection('A'), connection('B')];
  const first = h.context.activateConnection('A');
  const second = h.context.activateConnection('B');
  assert.equal(requests.length, 1);
  pendingA.resolve(connection('A', { is_active: true }));
  await first;
  assert.equal(requests.length, 2);
  pendingB.resolve(connection('B', { is_active: true }));
  await second;
  assert.equal(h.state.activeId, 'B');
  assert.equal(h.state.items.find((item) => item.id === 'B').is_active, true);
});

test('chat and tool tests use separate routes and versioned results', async () => {
  const oldProbe = deferred();
  const version = '2026-11-08T00:00:00Z';
  const h = setup((url) => {
    if (url.endsWith('/test-chat')) return Promise.resolve({ passed: true, message: '对话通过', connection_version: version });
    assert.match(url, /\/test-tools$/);
    return oldProbe.promise;
  });
  h.state.items = [connection('A')];
  await h.context.testConnection('A', 'chat');
  const old = h.context.testConnection('A', 'tool');
  oldProbe.resolve({ passed: false, message: '模型未调用工具', connection_version: version });
  await old;
  assert.equal(h.state.testResults.get('A:chat').passed, true);
  assert.equal(h.state.testResults.get('A:tool').passed, false);
  const later = deferred();
  h.context.modelRequestJson = () => later.promise;
  const stale = h.context.testConnection('A', 'chat');
  h.state.items = [connection('A', { updated_at: 'new-version' })];
  later.resolve({ passed: true, message: '过时结果', connection_version: version });
  await stale;
  assert.notEqual(h.state.testResults.get('A:chat').message, '过时结果');
});

test('a list response started before a mutation cannot overwrite newer state', async () => {
  const list = deferred();
  const activate = deferred();
  const h = setup((url, options) => options?.method === 'PUT' ? activate.promise : list.promise);
  h.state.items = [connection('A'), connection('B')];
  const loading = h.context.loadConnections();
  const switching = h.context.activateConnection('B');
  activate.resolve(connection('B', { is_active: true }));
  await switching;
  list.resolve({ items: [connection('A', { is_active: true }), connection('B')], active_connection_id: 'A' });
  await loading;
  assert.equal(h.state.activeId, 'B');
});

test('a save, deletion, and activation preserve their own updates when a list arrives during the writes', async () => {
  const save = deferred();
  const list = deferred();
  const calls = [];
  const h = setup(async (url, options) => {
    calls.push(options?.method || 'GET');
    if (options?.method === 'PATCH') return save.promise;
    if (options?.method === 'DELETE') return null;
    if (options?.method === 'PUT') return connection('A', { is_active: true });
    return list.promise;
  });
  h.state.items = [connection('A'), connection('B', { is_active: true })];
  h.state.activeId = 'B';
  h.state.editingId = 'A';
  validForm(h);
  const saving = h.context.saveConnection();
  h.context.openDeleteDialog('B');
  const deleting = h.context.confirmDelete();
  const switching = h.context.activateConnection('A');
  const loading = h.context.loadConnections();
  list.resolve({ items: [connection('A'), connection('B', { is_active: true })], active_connection_id: 'B' });
  assert.equal(await loading, false);
  save.resolve(connection('A', { name: '已保存' }));
  await Promise.all([saving, deleting, switching]);
  assert.deepEqual(calls, ['PATCH', 'GET', 'DELETE', 'PUT']);
  assert.equal(h.state.items.length, 1);
  assert.equal(h.state.items[0].name, '已保存');
  assert.equal(h.state.activeId, 'A');
});
