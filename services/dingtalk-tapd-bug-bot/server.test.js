'use strict';

// 覆盖 Bug Agent 的签名、安全边界、TAPD 字段映射和人工确认流程。

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {
  buildDescription,
  buildAgentDraftCard,
  buildInteractiveDraftParams,
  buildTapdPayload,
  createFormState,
  createAndDeliverInteractiveDraft,
  createServer,
  dingTalkApiRequest,
  extractIterations,
  extractReleases,
  extractUsers,
  getAgentStatus,
  handleAgentEvent,
  getConfig,
  mergeCardCallbackParams,
  mergeInteractiveCardInput,
  normalizeAgentDraft,
  parseJsonObject,
  ensureBugTitleModulePrefix,
  renderTapdDescriptionHtml,
  verifyAgentEventSignature,
  uploadTapdAttachment,
  verifyCardCallbackSignature,
  verifyDingTalkSignature,
  verifyFormState
} = require('./server');

test('verifies DingTalk HMAC signature and rejects stale requests', () => {
  const secret = 'ding-secret';
  const timestamp = String(Date.now());
  const sign = crypto.createHmac('sha256', secret)
    .update(`${timestamp}\n${secret}`)
    .digest('base64');
  assert.equal(verifyDingTalkSignature(timestamp, sign, secret).ok, true);
  assert.equal(verifyDingTalkSignature(String(Date.now() - 3700000), sign, secret).ok, false);
});

test('verifies signed DWS bridge events against the raw request body', () => {
  const secret = 'agent-secret';
  const timestamp = String(Date.now());
  const rawBody = Buffer.from('{"eventId":"c:m","messageId":"m","conversationId":"c","media":[]}');
  const signature = crypto.createHmac('sha256', secret)
    .update(`${timestamp}\n`)
    .update(rawBody)
    .digest('hex');
  const request = { headers: {
    'x-agent-timestamp': timestamp,
    'x-agent-signature': signature
  } };
  assert.equal(verifyAgentEventSignature(request, rawBody, { agentIngestSecret: secret }).ok, true);
  assert.equal(verifyAgentEventSignature(request, Buffer.from('{}'), { agentIngestSecret: secret }).ok, false);
  assert.equal(verifyAgentEventSignature(request, rawBody, { agentIngestSecret: 'wrong' }).ok, false);
});

test('rejects unsigned bridge events before background processing', async () => {
  const app = createServer({
    port: 0,
    agentIngestSecret: 'agent-secret',
    enableBugAgent: true
  });
  await new Promise((resolve) => app.listen(0, '127.0.0.1', resolve));
  const address = app.address();
  try {
    const response = await fetch(`http://127.0.0.1:${address.port}/api/agent/events`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ eventId: 'c:m', messageId: 'm', conversationId: 'c', media: [] })
    });
    const result = await response.json();
    assert.equal(response.status, 401);
    assert.equal(result.error, 'agent_timestamp_out_of_range');
  } finally {
    await new Promise((resolve) => app.close(resolve));
  }
});

test('builds a TAPD payload with required and optional fields', () => {
  const config = { tapdWorkspaceId: '1001', defaultPriorityLabel: '中' };
  const payload = buildTapdPayload({
    title: '白屏',
    actual: '提交后白屏',
    steps: '打开页面并提交',
    expected: '进入首页',
    severity: 'serious',
    module: '登录'
  }, config);
  assert.deepEqual(payload, {
    workspace_id: 1001,
    title: '【登录】白屏',
    description: '<p>【现象】<br>提交后白屏<br><br>【复现步骤】<br>打开页面并提交<br><br>【期望结果】<br>进入首页</p>',
    priority_label: '中',
    severity: 'serious',
    module: '登录'
  });
});

test('form state is signed, expires, and carries the DingTalk session', () => {
  const secret = 'form-secret';
  const now = Date.now();
  const token = createFormState({ workspaceId: '1001', senderStaffId: 'u1' }, secret, 'https://example.test/hook', now);
  const verified = verifyFormState(token, secret, now + 1000);
  assert.equal(verified.ok, true);
  assert.equal(verified.payload.workspaceId, '1001');
  assert.equal(verifyFormState(token, secret, now + 16 * 60 * 1000).ok, false);
});

test('description accepts a direct description', () => {
  assert.equal(buildDescription({ description: '已有完整描述' }), '已有完整描述');
});

test('appends media links to the TAPD description', () => {
  const payload = buildTapdPayload({ title: '带截图的 Bug', actual: '出现错误' }, {
    tapdWorkspaceId: '1001',
    defaultPriorityLabel: '中'
  }, null, [{ kind: 'image', url: 'https://example.test/media/demo.png' }]);
  assert.match(payload.description, /【截图】<br><img src="https:\/\/example\.test\/media\/demo\.png"/);
});

test('formats TAPD descriptions as HTML sections with inline images', () => {
  const payload = buildTapdPayload({
    title: '登录页白屏',
    module: '登录',
    description: '【现象】提交后白屏 【复现步骤】点击提交 【期望结果】进入首页'
  }, {
    tapdWorkspaceId: '1001',
    defaultPriorityLabel: '中'
  }, null, [{ kind: 'image', name: 'screen.png', url: 'https://example.test/screen.png' }]);
  assert.equal(payload.title, '【登录】登录页白屏');
  assert.match(payload.description, /^<p>【现象】提交后白屏<br><br>【复现步骤】点击提交<br><br>【期望结果】进入首页<\/p>/);
  assert.match(payload.description, /<p>【截图】<br><img src="https:\/\/example\.test\/screen\.png" alt="screen\.png"/);
});

test('normalizes Bug titles to the required module prefix', () => {
  assert.equal(ensureBugTitleModulePrefix('登录页白屏', 'Agent Team'), '【Agent Team】登录页白屏');
  assert.equal(ensureBugTitleModulePrefix('【旧模块】登录页白屏', 'Agent Team'), '【Agent Team】登录页白屏');
  assert.equal(ensureBugTitleModulePrefix('问题描述', ''), '【待确认模块】问题描述');
});

test('renders section labels and newlines for TAPD HTML descriptions', () => {
  const html = renderTapdDescriptionHtml({
    description: '【现象】A\n【复现步骤】B\n【期望结果】C'
  });
  assert.equal(html, '<p>【现象】A<br><br>【复现步骤】B<br><br>【期望结果】C</p>');
});

test('unwraps TAPD iteration response items', () => {
  const iterations = extractIterations({
    data: [{ Iteration: { id: '123', name: '企业知识中心 9 月' } }]
  });
  assert.deepEqual(iterations, [{ value: '123', label: '企业知识中心 9 月' }]);
});

test('unwraps TAPD release plan response items', () => {
  const releases = extractReleases({
    data: [{ Release: { id: 'r1', name: '9 月正式发布' } }]
  });
  assert.deepEqual(releases, [{ value: 'r1', label: '9 月正式发布' }]);
});

test('maps TAPD project members to display options', () => {
  const users = extractUsers({
    data: [
      { User: { user: 'dongchao', name: '董超' } },
      { User: { user: 'endeavor', name: '杨耀发' } }
    ]
  });
  assert.deepEqual(users, [
    { value: 'dongchao', label: '董超（dongchao）' },
    { value: 'endeavor', label: '杨耀发（endeavor）' }
  ]);
});

test('maps current TAPD UserWorkspace member responses', () => {
  const users = extractUsers({
    data: [
      { UserWorkspace: { user: 'liuming', name: '刘明', user_id: 'u1' } }
    ]
  });
  assert.deepEqual(users, [{ value: 'liuming', label: '刘明（liuming）' }]);
});

test('parses fenced JSON from the model', () => {
  assert.deepEqual(parseJsonObject('```json\n{"title":"白屏"}\n```'), { title: '白屏' });
});

test('normalizes model draft choices against TAPD options', () => {
  const draft = normalizeAgentDraft({
    title: '导入报错',
    description: '上传后报错',
    module: '导入',
    version_report: 'v1.2.0',
    iteration: '迭代一',
    severity: 'serious',
    confidence: 0.9
  }, {
    module: [{ value: 'm1', label: '导入' }],
    version_report: [{ value: 'v1', label: 'v1.2.0' }],
    iterations: [{ value: 'i1', label: '迭代一' }]
  }, []);
  assert.equal(draft.module, 'm1');
  assert.equal(draft.version_report, 'v1');
  assert.equal(draft.iteration_id, 'i1');
});

test('default config uses TAPD public API', () => {
  const config = getConfig();
  assert.equal(config.tapdApiBaseUrl, 'https://api.tapd.cn');
});

test('agent status never exposes secret values', () => {
  const status = getAgentStatus({
    enableBugAgent: true,
    openaiApiKey: 'secret-openai',
    openaiModel: 'gpt-5.6',
    openaiBaseUrl: 'https://api.openai.com/v1',
    dingTalkAccessToken: 'secret-ding',
    dingTalkAppKey: 'app-key',
    dingTalkRobotCode: 'robot-code',
    tapdAccessToken: 'secret-tapd',
    tapdWorkspaceId: '57379524'
  });
  assert.equal(status.openai.configured, true);
  assert.equal('apiKey' in status.openai, false);
  assert.equal(status.dingTalkMedia.accessTokenConfigured, true);
});

test('verifies interactive card callback signature', () => {
  const secret = 'card-secret';
  const timestamp = String(Date.now());
  const sign = require('node:crypto').createHmac('sha256', secret).update(timestamp).digest('base64');
  const request = { headers: {
    'x-ddpaas-signature-timestamp': timestamp,
    'x-ddpaas-signature': sign
  } };
  assert.equal(verifyCardCallbackSignature(request, { cardCallbackSecret: secret }).ok, true);
  assert.equal(verifyCardCallbackSignature(request, { cardCallbackSecret: 'wrong' }).ok, false);
});

test('builds interactive card parameters for AI-selected fields', () => {
  const params = buildInteractiveDraftParams({
    title: '提交后白屏',
    module_label: '登录',
    version_report_label: 'v1.2.0',
    iteration_label: '登录迭代',
    priority_label: '高',
    severity: 'serious',
    description: '【现象】提交后白屏',
    confidence: 0.92,
    media: [{ kind: 'image' }],
    id: 'draft-1'
  });
  assert.equal(params.priority_label, '高');
  assert.equal(params.severity, '严重');
  assert.equal(params.iteration, '登录迭代');
  assert.equal(params.media_count, '1');
  assert.equal(params.moduleName, '登录');
  assert.equal(params.versionReport, 'v1.2.0');
  assert.equal(params.releaseFlag, '待确认');
  assert.equal(params.draftId, 'draft-1');
  assert.equal(params.statusText, '请检查草稿，点击确认后才会创建 TAPD Bug。');
  assert.equal(params.submitButtonStatus, 'normal');
  assert.equal(params.confirmButtonVisible, 'true');
  assert.equal(typeof params.versionReportOptions, 'string');
  assert.equal(typeof params.versionReportIndex, 'string');
  assert.equal(typeof params.severityIndex, 'string');
  assert.deepEqual(JSON.parse(params.versionReportOptions), []);
});

test('maps non-action card status to a disabled submit button', () => {
  const { buildInteractiveCardParams } = require('./server');
  const params = buildInteractiveCardParams({ title: 'T', description: 'D' }, {
    status: '已取消',
    status_detail: '本次草稿未创建 TAPD Bug。'
  });
  assert.equal(params.state, '已取消');
  assert.equal(params.statusText, '本次草稿未创建 TAPD Bug。');
  assert.equal(params.submitButtonStatus, 'disabled');
  assert.equal(params.confirmButtonVisible, 'false');
});

test('keeps interactive card parameter values as strings for DingTalk', () => {
  const { buildInteractiveCardParams } = require('./server');
  const params = buildInteractiveCardParams({ title: 'T', description: 'D' }, {
    status: '创建中',
    status_detail: '正在写入 TAPD，请稍候。'
  });
  assert.equal(Object.values(params).every((value) => typeof value === 'string'), true);
});

test('serializes select options using the exported template locale shape', () => {
  const params = buildInteractiveDraftParams({
    title: '截图问题',
    module: 'm1',
    module_label: '登录',
    options: { module: [{ value: 'm1', label: '登录' }] }
  });
  const options = JSON.parse(params.moduleNameOptions);
  assert.equal(options[0].key, 'm1');
  assert.equal(options[0].value, 'm1');
  assert.equal(options[0].label, '登录');
  assert.equal(options[0].text.zh_CN, '登录');
  assert.equal(options[0].text.en_US, '登录');
});

test('uploads a TAPD attachment as multipart form data', async () => {
  const mediaDir = await fs.mkdtemp(path.join(os.tmpdir(), 'tapd-bug-bot-'));
  const filePath = path.join(mediaDir, 'screenshot.png');
  await fs.writeFile(filePath, Buffer.from('png-bytes'));
  let requestBody = '';
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requestBody = Buffer.concat(chunks).toString('utf8');
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ status: 1, data: { Attachment: { id: 'a-1', filename: 'screenshot.png' } } }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const result = await uploadTapdAttachment({
    filePath,
    name: '截图 1.png',
    contentType: 'image/png',
    kind: 'image'
  }, '1001', '2002', {
    mediaDir,
    tapdApiBaseUrl: `http://127.0.0.1:${address.port}`,
    tapdAccessToken: 'test-token',
    tapdClientId: '',
    tapdClientSecret: '',
    tapdAttachmentType: 'bug',
    tapdAttachmentCustomField: '',
    tapdAttachmentOwner: ''
  });
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  await fs.rm(mediaDir, { recursive: true, force: true });
  assert.equal(result.id, 'a-1');
  assert.match(requestBody, /name="workspace_id"/);
  assert.match(requestBody, /1001/);
  assert.match(requestBody, /name="entry_id"/);
  assert.match(requestBody, /2002/);
  assert.match(requestBody, /name="type"/);
  assert.match(requestBody, /bug/);
  assert.doesNotMatch(requestBody, /name="custom_field"/);
  assert.match(requestBody, /name="file"/);
  assert.match(requestBody, /png-bytes/);
});

test('creates the Bug before uploading its media attachments', async () => {
  const mediaDir = await fs.mkdtemp(path.join(os.tmpdir(), 'tapd-bug-bot-flow-'));
  const tapdPaths = [];
  const tapd = http.createServer(async (request, response) => {
    tapdPaths.push(request.url);
    for await (const _chunk of request) { /* drain request body */ }
    response.writeHead(200, { 'content-type': 'application/json' });
    if (request.url === '/bugs') {
      response.end(JSON.stringify({ status: 1, data: { Bug: { id: 'bug-1' } } }));
    } else {
      response.end(JSON.stringify({ status: 1, data: { Attachment: { id: 'attachment-1' } } }));
    }
  });
  const config = {
    port: 0,
    publicBaseUrl: 'http://127.0.0.1',
    mediaDir,
    tapdWorkspaceId: '1001',
    tapdApiBaseUrl: '',
    tapdAccessToken: 'test-token',
    tapdClientId: '',
    tapdClientSecret: '',
    tapdApiUser: '',
    tapdApiPassword: '',
    tapdAttachmentType: 'bug',
    tapdAttachmentCustomField: '',
    tapdAttachmentOwner: '',
    defaultPriorityLabel: '中',
    tapdBugUrlTemplate: 'https://tapd.example/{id}',
    formSecret: '',
    enableBugAgent: false,
    cardTemplateId: '',
    cardCallbackRouteKey: '',
    cardCallbackSecret: '',
    dingTalkAccessToken: '',
    dingTalkAppKey: '',
    dingTalkAppSecret: '',
    openaiApiKey: '',
    mockTapd: false
  };
  const app = createServer(config);
  await new Promise((resolve) => tapd.listen(0, '127.0.0.1', resolve));
  const tapdAddress = tapd.address();
  config.tapdApiBaseUrl = `http://127.0.0.1:${tapdAddress.port}`;
  await new Promise((resolve) => app.listen(0, '127.0.0.1', resolve));
  const appAddress = app.address();
  try {
    const response = await fetch(`http://127.0.0.1:${appAddress.port}/api/bugs`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        workspace_id: '1001',
        title: '截图 Bug',
        actual: '实际现象',
        media: [{ name: 'screenshot.png', data: 'data:image/png;base64,cG5nLWJ5dGVz' }]
      })
    });
    const result = await response.json();
    assert.equal(response.status, 200);
    assert.equal(result.bugId, 'bug-1');
    assert.equal(result.attachmentsUploaded, 1);
    assert.deepEqual(tapdPaths, ['/bugs', '/files/upload_attachment']);
  } finally {
    await new Promise((resolve) => app.close(resolve));
    await new Promise((resolve) => tapd.close(resolve));
    await fs.rm(mediaDir, { recursive: true, force: true });
  }
});

test('retries transient DingTalk system busy responses', async () => {
  let requests = 0;
  const server = http.createServer((request, response) => {
    requests += 1;
    response.writeHead(requests === 1 ? 500 : 200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(requests === 1
      ? { code: 'system.busy', message: 'system.busy' }
      : { result: 'ok' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  try {
    const result = await dingTalkApiRequest('/v1/test', 'POST', { ok: true }, {
      dingTalkAccessToken: 'test-token',
      dingTalkApiBaseUrl: `http://127.0.0.1:${address.port}`,
      dingTalkApiRetryAttempts: 2
    });
    assert.deepEqual(result, { result: 'ok' });
    assert.equal(requests, 2);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('keeps draft correlation when the post-delivery card refresh is busy', async () => {
  const calls = [];
  const server = http.createServer(async (request, response) => {
    calls.push(request.url);
    for await (const _chunk of request) { /* drain request body */ }
    response.writeHead(request.url.endsWith('/instances') && request.method === 'PUT' ? 500 : 200, {
      'content-type': 'application/json'
    });
    response.end(JSON.stringify(request.url.endsWith('/instances') && request.method === 'PUT'
      ? { code: 'system.busy', message: 'system.busy' }
      : { ok: true }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const draft = {
    id: 'draft-1',
    title: '【登录】白屏',
    description: '【现象】白屏',
    module_label: '登录',
    media: [],
    options: { module: [], version_report: [], iterations: [], release_plans: [], users: [] },
    senderStaffId: 'staff-1'
  };
  const config = {
    cardTemplateId: 'template',
    cardCallbackRouteKey: 'route',
    cardCallbackSecret: 'secret',
    dingTalkRobotCode: 'robot',
    dingTalkAccessToken: 'token',
    dingTalkApiBaseUrl: `http://127.0.0.1:${address.port}`,
    dingTalkApiRetryAttempts: 1
  };
  try {
    const outTrackId = await createAndDeliverInteractiveDraft(draft, config);
    assert.equal(outTrackId, 'tapd-bug-draft-draft-1');
    assert.equal(draft.cardOutTrackId, outTrackId);
    assert.equal(draft.cardRecipient, 'staff-1');
    assert.deepEqual(calls, [
      '/v1.0/card/instances',
      '/v1.0/card/instances/deliver',
      '/v1.0/card/instances'
    ]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('merges JSON encoded select callbacks and edited text', () => {
  const draft = {
    title: '旧标题',
    description: '旧描述',
    module: '',
    module_label: '',
    version_report: '',
    version_report_label: '',
    iteration_id: '',
    iteration_label: '',
    release_id: '',
    release_label: '',
    priority_label: '中',
    severity: 'normal',
    current_owner: '',
    de: '',
    te: '',
    options: {
      module: [{ value: 'm1', label: '登录' }],
      version_report: [{ value: 'v1', label: 'v1.2.0' }],
      iterations: [{ value: 'i1', label: '迭代一' }],
      release_plans: [{ value: 'r1', label: '9 月发布' }],
      users: []
    }
  };
  mergeInteractiveCardInput(draft, {
    title: '新标题',
    editableContent: '新描述',
    moduleName: '{"index":0,"value":"m1"}',
    versionReport: { index: 0, value: 'v1' },
    iterationId: '{"index":0,"value":"i1"}',
    releaseFlag: '{"index":0,"value":"r1"}'
  });
  assert.equal(draft.title, '【登录】新标题');
  assert.equal(draft.description, '新描述');
  assert.equal(draft.module, 'm1');
  assert.equal(draft.module_label, '登录');
  assert.equal(draft.version_report, 'v1');
  assert.equal(draft.version_report_label, 'v1.2.0');
  assert.equal(draft.iteration_id, 'i1');
  assert.equal(draft.iteration_label, '迭代一');
  assert.equal(draft.release_id, 'r1');
  assert.equal(draft.release_label, '9 月发布');
});

test('accepts locale-map labels from copied card select callbacks', () => {
  const draft = {
    module: '',
    module_label: '',
    options: { module: [{ value: 'm1', label: '登录' }] }
  };
  mergeInteractiveCardInput(draft, {
    moduleName: { text: { zh_CN: '登录' } }
  });
  assert.equal(draft.module, 'm1');
  assert.equal(draft.module_label, '登录');
});

test('decodes stringified card callback values', () => {
  const { parseCardCallbackValue } = require('./server');
  assert.deepEqual(parseCardCallbackValue('{"action":"confirm"}'), { action: 'confirm' });
  assert.deepEqual(parseCardCallbackValue({ action: 'cancel' }), { action: 'cancel' });
});

test('merges public and recipient-private card callback params', () => {
  const merged = mergeCardCallbackParams(
    { moduleName: '{"index":0,"value":"m1"}' },
    { params: JSON.stringify({ versionReport: { index: 1, value: 'v2' } }) },
    { params: { moduleName: '{"index":1,"value":"m2"}' } }
  );
  assert.equal(merged.versionReport.value, 'v2');
  assert.equal(merged.moduleName, '{"index":1,"value":"m2"}');
});
