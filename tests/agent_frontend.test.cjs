const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../src/local_workspace_service/static/agent.js'), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));

function setup(request) {
  const elements = new Map();
  function element() {
    return {
      textContent: '', children: [], value: '', disabled: false, open: false,
      classList: { toggle() {} },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
      addEventListener(name, handler) { this[name] = handler; },
      showModal() { this.open = true; },
      close() { this.open = false; },
    };
  }
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  let nextKey = 0;
  const context = vm.createContext({
    document: { getElementById: get, createElement: element, addEventListener() {} },
    crypto: { randomUUID: () => `request-${++nextKey}` }, setTimeout,
  });
  vm.runInContext(source, context);
  context.agentJson = request;
  const state = vm.runInContext('agentState', context);
  state.connection = { id: 'connection', name: '连接', model: 'demo', updated_at: 'v1', base_url: 'https://example.test/v1' };
  get('agent-workspace').value = 'A';
  get('agent-question').value = '待办事项？';
  state.enabledFingerprint = context.fingerprint();
  return { state, context, get };
}

const task = (id, workspace, status, extra = {}) => ({
  task_id: id, workspace_id: workspace, status, events: [], citations: [], ...extra,
});

test('duplicate click is ignored while polling, and a failed task permits retry', async () => {
  let writes = 0;
  let finish;
  const oldPoll = new Promise((resolve) => { finish = resolve; });
  const h = setup(async (url, options) => {
    if (options?.method === 'POST') {
      const body = JSON.parse(options.body);
      assert.equal(body.connection_version, 'v1');
      return ++writes === 1 ? task('old', 'A', 'queued') : task('new', 'A', 'succeeded', { answer: '已恢复' });
    }
    return oldPoll;
  });
  const sending = h.context.sendQuestion();
  await tick();
  await h.context.sendQuestion();
  assert.equal(writes, 1);
  finish(task('old', 'A', 'failed', { error_message: '模拟错误' }));
  await sending;
  assert.equal(h.get('send-question').disabled, false);
  assert.equal(h.get('agent-message').textContent, '模拟错误');
  await h.context.sendQuestion();
  assert.equal(writes, 2);
  assert.equal(h.get('agent-answer').textContent, '已恢复');
});

test('late results from a different workspace cannot overwrite the answer or citation view', async () => {
  let finishOld;
  const oldPoll = new Promise((resolve) => { finishOld = resolve; });
  const excerpt = '<img src=x onerror=alert(1)>';
  const h = setup(async (url, options) => {
    if (url.endsWith('/sources/src_1')) return { relative_path: 'notes.md', location: '第 2 行', version: 'abcdef', excerpt };
    if (url.endsWith('/old')) return oldPoll;
    const body = JSON.parse(options.body);
    if (url.endsWith('/cloud-enable')) return { connection_version: 'v1', base_url: 'https://example.test/v1' };
    return body.workspace_id === 'A'
      ? task('old', 'A', 'queued')
      : task('new', 'B', 'succeeded', { answer: '工作区 B', citations: [{ source_id: 'src_1', relative_path: 'notes.md', location: '第 2 行' }] });
  });
  const sending = h.context.sendQuestion();
  await tick();
  h.get('agent-workspace').value = 'B';
  h.context.changeAgentContext();
  await h.context.enableCloud();
  await h.context.sendQuestion();
  finishOld(task('old', 'A', 'succeeded', { answer: '过时回答' }));
  await sending;
  assert.equal(h.get('agent-answer').textContent, '工作区 B');
  await h.get('agent-citations').children[0].click();
  assert.equal(h.get('source-dialog').open, true);
  assert.equal(h.get('source-text').textContent, excerpt);
});
