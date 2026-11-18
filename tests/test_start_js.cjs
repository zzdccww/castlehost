const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const {test} = require('node:test');
const path = require('node:path');

// Execute the actual scripts emitted by renew.py with an isolated DOM/jQuery fixture.
const exported = spawnSync('python', ['-c', [
  'import json, renew',
  'class Browser:',
  '    def execute_script(self, script, *args): return script',
  'print(json.dumps({"observer": renew._OBSERVE_START_JS, "state": renew.CastleClient(Browser(), 0).check_server_stopped("12345")}))',
].join('\n')], {cwd: path.resolve(__dirname, '..'), encoding: 'utf8'});
assert.equal(exported.status, 0, exported.stderr);
const scripts = JSON.parse(exported.stdout);

function browser(onclicks = []) {
  let handler;
  const document = {
    querySelector: () => ({content: 'private-csrf'}),
    querySelectorAll: () => onclicks.map(onclick => ({getAttribute: () => onclick})),
    createElement: () => ({
      set innerHTML(value) { this.textContent = value.replace(/<[^>]*>/g, ''); },
    }),
  };
  const hooks = {
    off() { handler = undefined; return hooks; },
    on(event, callback) { handler = callback; return hooks; },
  };
  const window = {jQuery: () => hooks};
  const location = {href: 'https://cp.castle-host.com/servers'};
  const execute = (script, ...args) => new Function('window', 'document', 'jQuery', 'location',
    'return function() {' + script + '\n};')(window, document, window.jQuery, location)(...args);
  return {
    window, execute,
    complete(url, payload, status = 200) {
      if (handler) handler({}, {status, responseText: typeof payload === 'string' ? payload : JSON.stringify(payload)}, {url});
    },
  };
}

test('captures HTTP 200 business rejection without exposing response secrets', () => {
  const b = browser();
  assert.equal(b.execute(scripts.observer, '12345', 'start'), true);
  b.complete('/servers/control/action/12345/start', {
    status: 'error', error: '<b>Ошибка при запуске сервера</b> 12345 private-csrf 192.0.2.5',
    csrf_token: 'response-secret', uid: 'account-secret',
  });
  const response = b.window.__castleStartResponse;
  assert.equal(response.http_status, 200);
  assert.equal(response.status, 'error');
  assert.equal(response.message, 'Ошибка при запуске сервера [server] [csrf] [address]');
  assert.ok(!JSON.stringify(response).includes('secret'));
  assert.equal(response.json, true);
});

test('matches only this server and action, and captures the first response', () => {
  const b = browser();
  b.execute(scripts.observer, '12345', 'start');
  for (const url of ['/servers/control/status/12345', '/servers/control/action/67890/start',
    '/servers/control/action/12345/restart']) b.complete(url, {status: 'success'});
  assert.equal(b.window.__castleStartResponse, null);
  b.complete('https://cp.castle-host.com/servers/control/action/12345/start/?x=1', {status: 'error', error: 'first'});
  b.complete('/servers/control/action/12345/start', {status: 'success', success: 'second'});
  assert.equal(b.window.__castleStartResponse.message, 'first');
});

test('distinguishes CAPTCHA, non JSON challenge, and transport failure', () => {
  const b = browser();
  b.execute(scripts.observer, '12345', 'start');
  b.complete('/servers/control/action/12345/start', {status: 'captcha_required'});
  assert.equal(b.window.__castleStartResponse.captcha_required, true);
  b.execute(scripts.observer, '12345', 'start');
  b.complete('/servers/control/action/12345/start', '<html>turnstile private-csrf</html>', 403);
  assert.equal(b.window.__castleStartResponse.challenge, true);
  assert.equal(b.window.__castleStartResponse.message, '');
  b.execute(scripts.observer, '12345', 'start');
  b.complete('/servers/control/action/12345/start', '<html>bad gateway</html>', 502);
  assert.equal(b.window.__castleStartResponse.json, false);
  assert.equal(b.window.__castleStartResponse.challenge, false);
  assert.equal(b.window.__castleStartResponse.http_status, 502);
});

test('running state requires explicit controls belonging to the requested server', () => {
  const fixtures = [
    [[], null],
    [["sendActionStatus(67890, 'start')", "sendActionStatus(12345, 'delete')"], null],
    [["sendActionStatus(12345, 'start')"], true],
    [['sendAction(12345, "stop")'], false],
    [["sendAction(12345, 'restart')"], false],
    [["sendAction(67890, 'start')", "sendAction(12345, 'kill')"], false],
  ];
  for (const [onclicks, expected] of fixtures) {
    assert.equal(browser(onclicks).execute(scripts.state, '12345'), expected, JSON.stringify(onclicks));
  }
});
