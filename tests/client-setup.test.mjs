import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { clientSetupCommands } from '../app/static/client-setup.mjs';

test('generates a copyable APT configuration and verification command', () => {
  const commands = clientSetupCommands('http://192.168.1.10:8000');
  assert.ok(commands.setup.startsWith(`printf '%s\\n' 'Acquire::http::Proxy "http://192.168.1.10:8000/";'`));
  assert.equal(spawnSync('sh', ['-n'], { input: commands.setup }).status, 0);
  assert.equal(commands.verify, "apt-config dump | grep -F 'Acquire::http::Proxy'");
  assert.equal(commands.remove, 'sudo rm -f /etc/apt/apt.conf.d/01squid-deb-proxy');
});

test('supports hostnames, IPv6, and the default HTTP port', () => {
  for (const proxy of ['http://proxy.lan:9000/', 'http://[fd00::1]:8000/', 'http://proxy.lan/']) {
    assert.ok(clientSetupCommands(proxy).setup.includes(`"${proxy}"`));
  }
});

test('rejects invalid endpoints, credentials, and shell injection', () => {
  for (const proxy of ['', 'hostname', 'https://proxy.lan:8000/', 'http://user:secret@proxy.lan/',
    'http://proxy.lan:99999/', 'http://proxy.lan/path', 'http://proxy.lan/?x=y',
    'http://proxy.lan/#fragment', "http://proxy.lan/'; touch /tmp/injected; '",
    'http://$(whoami):8000/', 'http://proxy.lan/\nsudo reboot', 'http://proxy.lan\\evil/']) {
    assert.throws(() => clientSetupCommands(proxy), Error, proxy);
  }
});

for (const [scenario, repository, download, expected] of [
  ['Docker repository configured', 0, 0, ['install', 'curl', 'chmod', 'apt-get']],
  ['no Docker repository', 1, 0, ['apt-get']],
  ['key download fails', 0, 1, ['install', 'curl']],
]) {
  test(`key refresh before apt update: ${scenario}`, () => {
    const setup = clientSetupCommands('http://proxy.lan:8000/').setup;
    // Shell functions intercept all system changes; no client configuration is written.
    const mocks = `
      grep() { return ${repository}; }
      sudo() {
        case "$1" in
          tee) cat > /dev/null ;;
          curl) echo curl; return ${download} ;;
          *) echo "$1" ;;
        esac
      }
    `;
    const result = spawnSync('sh', ['-c', mocks + setup], { encoding: 'utf8' });
    assert.deepEqual(result.stdout.trim().split('\n'), expected);
    assert.equal(result.status, download);
  });
}
