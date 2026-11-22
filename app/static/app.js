/* Live dashboard; all proxy-derived strings are inserted as text. */
import { clientSetupCommands } from './client-setup.mjs';
import { copyText } from './clipboard.mjs';
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
let recent = [];
let refreshing = false;

function bytes(value) {
  if (value === null || value === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text) {
  $('toast').textContent = text;
  $('toast').hidden = false;
  clearTimeout(message.timeout);
  message.timeout = setTimeout(() => { $('toast').hidden = true; }, 9000);
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, cache: 'no-store' });
  if (!response.ok) {
    if (response.status === 401) {
      window.location.replace('/login');
      throw new Error('Your session expired. Please sign in again.');
    }
    let detail = `Request failed (HTTP ${response.status})`;
    try { const body = await response.json(); if (typeof body.detail === 'string') detail = body.detail; else if (Array.isArray(body.detail)) detail = body.detail.map((item) => item.msg).join('; '); } catch { /* Non-JSON response. */ }
    throw new Error(detail);
  }
  return response;
}
function renderRequests() {
  const query = $('search').value.toLowerCase();
  const rows = recent.filter((row) => `${row.url} ${row.client} ${row.result}`.toLowerCase().includes(query));
  $('request-count').textContent = rows.length;
  const body = $('request-rows');
  body.replaceChildren();
  if (!rows.length) {
    const cell = element('td', query ? 'No requests match your filter.' : 'No requests yet. Connect an APT client to start filling your cache.', 'empty');
    cell.colSpan = 6;
    const row = element('tr');
    row.append(cell);
    body.append(row);
    return;
  }
  for (const row of rows) {
    const tr = element('tr');
    tr.append(element('td', new Date(row.timestamp * 1000).toLocaleTimeString()));
    const path = element('td', undefined, 'request-path');
    path.title = row.url;
    path.append(element('strong', row.url), element('small', `${row.method} · ${row.status}`));
    tr.append(path, element('td', row.client));
    const status = element('td');
    status.append(element('span', row.hit ? 'HIT' : row.error ? 'ERROR' : row.result.replace('TCP_', ''), `badge ${row.hit ? 'hit' : row.error ? 'error' : ''}`));
    tr.append(status, element('td', bytes(row.bytes)), element('td', `${number.format(row.duration)} ms`));
    body.append(tr);
  }
}
function renderRanks(id, rows, field, icon) {
  const target = $(id);
  target.replaceChildren();
  if (!rows.length) { target.append(element('div', 'Activity will appear after clients use the proxy.', 'rank-empty')); return; }
  for (const row of rows) {
    const line = element('div', undefined, 'rank-row');
    const label = element('span');
    label.append(element('i', icon, 'rank-icon'), document.createTextNode(row[field]));
    line.append(label, element('span', `${number.format(row.requests)} requests`, 'rank-count'));
    target.append(line);
  }
}
function renderChart(timeline) {
  const chart = $('chart');
  chart.replaceChildren();
  const max = Math.max(...timeline.map((bucket) => bucket.requests), 0);
  if (!max) { chart.append(element('div', 'Your traffic will appear here as requests arrive.', 'chart-empty')); return; }
  for (const bucket of timeline) {
    const group = element('div', undefined, 'chart-bucket');
    group.title = `${new Date(bucket.timestamp * 1000).toLocaleTimeString()}: ${bucket.requests} requests, ${bucket.hits} hits`;
    for (const [count, className] of [[bucket.requests, 'bar'], [bucket.hits, 'bar hit']]) {
      const bar = element('div', undefined, className);
      // Dynamic heights use the CSSOM, without interpreting proxy input as markup.
      bar.style.height = `${count / max * 100}%`;
      group.append(bar);
    }
    chart.append(group);
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  $('refresh').disabled = true;
  try {
    const data = await (await api('/api/overview')).json();
    const counters = data.counters;
    const activity = data.activity;
    $('proxy-state').textContent = data.online ? 'Proxy is online' : 'Proxy is unavailable';
    $('status-light').className = `status-light ${data.online ? 'online' : 'offline'}`;
    const warnings = [data.error, !activity.available ? 'Access log is not readable. Request activity is unavailable.' : null].filter(Boolean);
    $('error').hidden = !warnings.length;
    $('error').textContent = warnings.join(' ');
    const total = counters['client_http.requests'];
    const hits = counters['client_http.hits'];
    $('total').textContent = total === undefined ? '—' : number.format(total);
    $('hit-ratio').textContent = total === undefined || hits === undefined ? '—' : `${(total ? hits / total * 100 : 0).toFixed(1)}%`;
    $('served').textContent = bytes(counters['client_http.kbytes_out'] === undefined ? null : counters['client_http.kbytes_out'] * 1024);
    $('client-count').textContent = activity.available ? number.format(activity.client_count) : '—';
    const storage = data.storage;
    $('disk-used').textContent = bytes(storage?.used_bytes);
    $('disk-capacity').textContent = storage?.used_bytes == null ? 'Storage data unavailable'
      : storage.capacity_bytes > 0
        ? `${(storage.used_bytes / storage.capacity_bytes * 100).toFixed(1)}% of ${bytes(storage.capacity_bytes)} cache limit`
        : storage.capacity_bytes === 0 ? 'Disk cache disabled' : 'Cache limit unavailable';
    $('hour-total').textContent = `${number.format(activity.requests)} requests`;
    $('saved').textContent = activity.available ? bytes(activity.saved_bytes) : '—';
    $('hour-hits').textContent = activity.available ? number.format(activity.hits) : '—';
    $('hour-errors').textContent = activity.available ? number.format(activity.errors) : '—';
    $('efficiency-fill').style.width = `${activity.requests ? activity.hits / activity.requests * 100 : 0}%`;
    $('updated').textContent = `Updated ${new Date(data.fetched_at * 1000).toLocaleTimeString()}`;
    $('reload').disabled = !data.online;
    recent = activity.recent;
    renderRequests();
    renderChart(activity.timeline);
    renderRanks('clients-list', activity.clients, 'client', '▦');
    renderRanks('hosts-list', activity.hosts, 'host', '↗');
  } catch (error) {
    $('disk-used').textContent = '—';
    $('disk-capacity').textContent = 'Storage data unavailable';
    $('error').hidden = false;
    $('error').textContent = `${error.message} Displayed values may be stale.`;
    $('proxy-state').textContent = 'Connection interrupted';
    $('status-light').className = 'status-light offline';
    $('reload').disabled = true;
  } finally { refreshing = false; $('refresh').disabled = false; }
}
async function loadReport() {
  $('report-body').textContent = 'Loading live report…';
  try { $('report-body').textContent = await (await api(`/api/reports/${$('report-select').value}`)).text(); }
  catch (error) { $('report-body').textContent = error.message; }
}
$('refresh').addEventListener('click', refresh);
$('logout').addEventListener('click', async () => {
  $('logout').disabled = true;
  try {
    await api('/api/logout', { method: 'POST', headers: { 'X-Requested-With': 'squid-ui' } });
    window.location.replace('/login');
  } catch (error) { message(error.message); }
  finally { $('logout').disabled = false; }
});
$('search').addEventListener('input', renderRequests);
$('diagnostics').addEventListener('click', () => { $('report-dialog').showModal(); loadReport(); });
$('close-dialog').addEventListener('click', () => $('report-dialog').close());
$('report-select').addEventListener('change', loadReport);
$('reload').addEventListener('click', () => $('reload-dialog').showModal());
$('cancel-reload').addEventListener('click', () => $('reload-dialog').close());
$('confirm-reload').addEventListener('click', async () => {
  $('confirm-reload').disabled = true;
  try {
    const data = await (await api('/api/actions/reconfigure', { method: 'POST', headers: { 'X-Requested-With': 'squid-ui' } })).json();
    $('reload-dialog').close(); message(data.message); setTimeout(refresh, 2000);
  } catch (error) { message(error.message); }
  finally { $('confirm-reload').disabled = false; }
});
function renderClientSetup() {
  try {
    const commands = clientSetupCommands($('proxy-url').value);
    $('setup-command').textContent = commands.setup;
    $('verify-command').textContent = commands.verify;
    $('remove-command').textContent = commands.remove;
    $('setup-error').hidden = true;
    $('proxy-url').setAttribute('aria-invalid', 'false');
    document.querySelectorAll('.copy-command').forEach((button) => { button.disabled = false; });
  } catch (error) {
    $('setup-error').textContent = error.message;
    $('setup-error').hidden = false;
    $('proxy-url').setAttribute('aria-invalid', 'true');
    for (const id of ['setup-command', 'verify-command', 'remove-command']) $(id).textContent = '';
    document.querySelectorAll('.copy-command').forEach((button) => { button.disabled = true; });
  }
}
$('proxy-url').value = `http://${window.location.hostname}:8000/`;
$('proxy-url').addEventListener('input', renderClientSetup);
document.querySelectorAll('.copy-command').forEach((button) => {
  button.addEventListener('click', async () => {
    const command = $(button.dataset.command);
    if (await copyText(command.textContent)) {
      message('Command copied. Paste it into a terminal on the APT client.');
    } else {
      command.focus();
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(command);
      selection.removeAllRanges();
      selection.addRange(range);
      const shortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘C' : 'Ctrl+C';
      message(`Automatic copying is blocked by your browser. Press ${shortcut} to copy the selected command.`);
    }
  });
});
renderClientSetup();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 10000);

let mirrorRevision = null;
let mirrorOriginal = '';
let mirrorBusy = false;
function mirrorStatus(text, kind = '') {
  $('mirrors-status').textContent = text;
  $('mirrors-status').className = kind;
}
function mirrorControls() {
  $('mirror-domains').disabled = mirrorBusy || !mirrorRevision;
  $('load-mirrors').disabled = mirrorBusy;
  $('save-mirrors').disabled = mirrorBusy || !mirrorRevision || $('mirror-domains').value === mirrorOriginal;
}
async function loadMirrors() {
  mirrorBusy = true;
  mirrorControls();
  mirrorStatus('Loading allowed mirrors…');
  try {
    const data = await (await api('/api/mirrors')).json();
    mirrorRevision = data.revision;
    mirrorOriginal = data.domains.join('\n');
    $('mirror-domains').value = mirrorOriginal;
    mirrorStatus(`${data.domains.length} allowed repository entries. Add or remove a line, then save to apply.`);
  } catch (error) { mirrorStatus(error.message, 'error'); }
  finally { mirrorBusy = false; mirrorControls(); }
}
$('load-mirrors').addEventListener('click', loadMirrors);
$('mirror-domains').addEventListener('input', mirrorControls);
$('mirrors-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (mirrorBusy || !mirrorRevision) return;
  const domains = $('mirror-domains').value.split('\n').map((line) => line.trim()).filter(Boolean);
  if (!domains.length || domains.length > 200) { mirrorStatus('Enter between 1 and 200 repository hostnames.', 'error'); return; }
  mirrorBusy = true;
  mirrorControls();
  mirrorStatus('Saving mirrors and requesting a Squid reload…');
  try {
    const data = await (await api('/api/mirrors', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'squid-ui' },
      body: JSON.stringify({ domains, revision: mirrorRevision }),
    })).json();
    mirrorRevision = data.revision;
    mirrorOriginal = data.domains.join('\n');
    $('mirror-domains').value = mirrorOriginal;
    mirrorStatus(data.message, 'success');
    setTimeout(refresh, 2000);
  } catch (error) { mirrorStatus(error.message, 'error'); }
  finally { mirrorBusy = false; mirrorControls(); }
});
loadMirrors();
