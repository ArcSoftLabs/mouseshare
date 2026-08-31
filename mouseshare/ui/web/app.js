/* MouseShare UI.
 *
 * Everything renders from one state snapshot pushed by Python. No local
 * copy of the truth, no per-event DOM patching -- render(state) is the
 * only way pixels change.
 */

let state = null;
let dragging = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const api = () => window.pywebview.api;

/* -- entry ------------------------------------------------------------ */

window.addEventListener('pywebviewready', async () => {
  wire();
  render(await api().ready());
  refreshPermissions();
});

// Python pushes every subsequent change through here.
window.onState = (next) => render(next);

/* -- rendering -------------------------------------------------------- */

function render(next) {
  if (!next) return;
  if (state && next.revision <= state.revision) return; // never rewind
  state = next;

  const screen = state.pairing ? 'pairing' : state.screen;
  $$('section').forEach((s) => s.classList.toggle('visible', s.dataset.screen === screen));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.screen === screen));

  const banner = $('#banner');
  banner.textContent = state.error || '';
  banner.hidden = !state.error;

  // A notice is worth saying but is not something going wrong, so it must
  // not look like a failure.
  const notice = $('#notice');
  notice.textContent = state.notice || '';
  notice.hidden = !state.notice;

  $('#self-name').textContent = state.device.name;
  $('#self-port').textContent = 'port ' + state.device.port;
  if (document.activeElement !== $('#name-input')) {
    $('#name-input').value = state.device.name;
  }

  renderPeers();
  renderPairing();
  renderLayout();
  renderPaired();
}

function renderPeers() {
  const host = $('#peers');
  host.innerHTML = '';
  $('#peers-empty').hidden = state.peers.length > 0;

  for (const peer of state.peers) {
    const card = el('div', 'card' + (peer.connected ? ' live' : ''));
    card.appendChild(el('div', 'icon', peer.online ? '🖥' : '💤'));

    const meta = el('div', 'meta');
    meta.appendChild(el('strong', '', peer.name));
    const sub = el('span');
    sub.appendChild(el('span', 'dot' + (peer.online ? ' on' : '')));
    // Not heard from is not the same as not reachable: say which it is,
    // and offer the address we would dial rather than a flat "Offline"
    // that reads as a dead end.
    sub.appendChild(document.createTextNode(
      peer.connected ? 'Connected'
        : peer.online ? peer.address
        : peer.reachable ? `Last seen at ${peer.address}`
        : 'No address yet'
    ));
    if (peer.paired) sub.appendChild(el('span', 'badge paired', 'paired'));
    meta.appendChild(sub);
    card.appendChild(meta);

    const button = el('button', peer.connected ? 'ghost' : 'primary',
                      peer.connected ? 'Disconnect' : 'Connect');
    // Reachable, not online. Discovery finds machines; it does not decide
    // whether one we already hold an address for can be dialled.
    button.disabled = !peer.reachable && !peer.connected;
    button.onclick = () => (peer.connected ? api().cancel() : api().connect(peer.device_id));
    card.appendChild(button);
    host.appendChild(card);
  }
}

function renderPairing() {
  const p = state.pairing;
  $('#pair-target').hidden = !(p && p.role === 'target');
  $('#pair-connector').hidden = !(p && p.role === 'connector');
  if (!p) return;

  if (p.role === 'target') {
    $('#pair-peer-name').textContent = p.peer || 'the other machine';
    const box = $('#code');
    box.innerHTML = '';
    for (const digit of String(p.code)) box.appendChild(el('span', '', digit));
    $('#pair-remaining').textContent = p.remaining;
  } else {
    const first = $('#code-entry input');
    if (document.activeElement.tagName !== 'INPUT') first.focus();
  }
}

/* -- layout canvas ---------------------------------------------------- */

function renderLayout() {
  const canvas = $('#canvas');
  const devices = (state.layout && state.layout.devices) || [];
  const screens = devices.flatMap((d) => d.monitors);
  $('#layout-hint').hidden = devices.length > 1;
  canvas.hidden = devices.length === 0;
  if (!screens.length) { canvas.innerHTML = ''; return; }
  if (dragging) return; // never re-render mid-drag

  // Fit the whole arrangement into the canvas with a margin.
  const minX = Math.min(...screens.map((m) => m.x));
  const minY = Math.min(...screens.map((m) => m.y));
  const maxX = Math.max(...screens.map((m) => m.x + m.w));
  const maxY = Math.max(...screens.map((m) => m.y + m.h));
  const pad = 46;
  const scale = Math.min(
    (canvas.clientWidth - pad * 2) / Math.max(maxX - minX, 1),
    (canvas.clientHeight - pad * 2) / Math.max(maxY - minY, 1)
  );
  const originX = (canvas.clientWidth - (maxX - minX) * scale) / 2 - minX * scale;
  const originY = (canvas.clientHeight - (maxY - minY) * scale) / 2 - minY * scale;

  canvas.innerHTML = '';
  for (const device of devices) {
    const dMinX = Math.min(...device.monitors.map((m) => m.x));
    const dMinY = Math.min(...device.monitors.map((m) => m.y));
    const dMaxX = Math.max(...device.monitors.map((m) => m.x + m.w));
    const dMaxY = Math.max(...device.monitors.map((m) => m.y + m.h));

    const block = el('div', 'block' + (device.device_id === state.device.id ? ' self' : ''));
    block.style.left = originX + dMinX * scale + 'px';
    block.style.top = originY + dMinY * scale + 'px';
    block.style.width = (dMaxX - dMinX) * scale + 'px';
    block.style.height = (dMaxY - dMinY) * scale + 'px';
    block.appendChild(el('div', 'label', device.name));

    for (const m of device.monitors) {
      const screen = el('div', 'screen' + (m.primary ? ' primary' : ''),
                        `${m.w}×${m.h}`);
      screen.style.left = (m.x - dMinX) * scale + 'px';
      screen.style.top = (m.y - dMinY) * scale + 'px';
      screen.style.width = m.w * scale - 3 + 'px';
      screen.style.height = m.h * scale - 3 + 'px';
      block.appendChild(screen);
    }

    block.addEventListener('pointerdown', (ev) => startDrag(ev, block, device, scale));
    canvas.appendChild(block);
  }
}

function startDrag(ev, block, device, scale) {
  ev.preventDefault();
  block.setPointerCapture(ev.pointerId);
  block.classList.add('dragging');
  dragging = {
    block, device, scale,
    startX: ev.clientX, startY: ev.clientY,
    originLeft: parseFloat(block.style.left),
    originTop: parseFloat(block.style.top),
  };

  const move = (e) => {
    dragging.block.style.left = dragging.originLeft + (e.clientX - dragging.startX) + 'px';
    dragging.block.style.top = dragging.originTop + (e.clientY - dragging.startY) + 'px';
  };

  const finish = async (e) => {
    block.removeEventListener('pointermove', move);
    block.removeEventListener('pointerup', finish);
    block.removeEventListener('pointercancel', abort);
    block.removeEventListener('lostpointercapture', abort);
    block.classList.remove('dragging');
    if (!dragging) return;
    const dx = Math.round((e.clientX - dragging.startX) / scale);
    const dy = Math.round((e.clientY - dragging.startY) / scale);
    const [ox, oy] = device.offset;
    dragging = null;
    // One call: Python snaps, validates and persists together, so the
    // picture can never disagree with the geometry the cursor crosses.
    render(await api().set_offset(device.device_id, ox + dx, oy + dy));
  };

  // A cancelled or lost pointer must clear the drag too, or renderLayout
  // refuses to draw anything ever again.
  const abort = () => {
    block.removeEventListener('pointermove', move);
    block.removeEventListener('pointerup', finish);
    block.removeEventListener('pointercancel', abort);
    block.removeEventListener('lostpointercapture', abort);
    block.classList.remove('dragging');
    dragging = null;
    renderLayout();
  };

  block.addEventListener('pointermove', move);
  block.addEventListener('pointerup', finish);
  block.addEventListener('pointercancel', abort);
  block.addEventListener('lostpointercapture', abort);
}

/* -- settings --------------------------------------------------------- */

function renderPaired() {
  const host = $('#paired');
  host.innerHTML = '';
  const paired = state.peers.filter((p) => p.paired);
  if (!paired.length) {
    host.appendChild(el('small', '', 'No paired machines yet.'));
    return;
  }
  for (const peer of paired) {
    const row = el('div', 'row');
    row.style.padding = '7px 0';
    row.appendChild(el('div', 'meta', peer.name));
    const forget = el('button', 'danger', 'Forget');
    forget.onclick = () => api().forget(peer.device_id);
    row.appendChild(forget);
    host.appendChild(row);
  }
}

async function refreshPermissions() {
  const perms = await api().permissions();
  const panel = $('#permissions-panel');
  panel.hidden = !perms.needed;
  if (!perms.needed) return;

  const host = $('#permissions');
  host.innerHTML = '';
  for (const item of perms.items) {
    const row = el('div', 'perm');
    row.appendChild(el('span', 'dot ' + (item.granted ? 'on' : 'bad')));
    const text = el('div', 'meta');
    text.appendChild(el('strong', '', item.label));
    text.appendChild(el('span', '', item.granted ? 'Granted' : item.why));
    row.appendChild(text);
    if (!item.granted) {
      const open = el('button', 'ghost', 'Open Settings');
      open.onclick = () => api().open_permissions(item.key);
      row.appendChild(open);
    }
    host.appendChild(row);
  }
}

// Granting a permission happens outside this window, so the panel has to
// re-check while the user is looking at it rather than only at startup.
setInterval(() => {
  if (state && state.screen === 'settings') refreshPermissions();
}, 2000);

/* -- wiring ----------------------------------------------------------- */

function wire() {
  $$('.tab').forEach((tab) => {
    tab.onclick = () => api().show(tab.dataset.screen).then(render);
  });
  $$('[data-action="cancel"]').forEach((b) => {
    b.onclick = () => api().cancel().then(render);
  });
  $('#manual-go').onclick = () =>
    api().connect_manually($('#manual-address').value).then(render);
  $('#name-save').onclick = () =>
    api().rename($('#name-input').value).then(render);
  $('#code-submit').onclick = submitCode;

  const boxes = $$('#code-entry input');
  boxes.forEach((box, i) => {
    box.addEventListener('input', () => {
      box.value = box.value.replace(/\D/g, '').slice(0, 1);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      if (boxes.every((b) => b.value)) submitCode();
    });
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && i > 0) boxes[i - 1].focus();
    });
    box.addEventListener('paste', (e) => {
      const digits = (e.clipboardData.getData('text') || '').replace(/\D/g, '');
      if (!digits) return;
      e.preventDefault();
      boxes.forEach((b, j) => (b.value = digits[j] || ''));
      if (digits.length >= 6) submitCode();
    });
  });

  window.addEventListener('resize', () => state && renderLayout());
}

async function submitCode() {
  const code = $$('#code-entry input').map((b) => b.value).join('');
  if (code.length !== 6) return;
  const next = await api().submit_code(code);
  $$('#code-entry input').forEach((b) => (b.value = ''));
  $('#code-entry input').focus();
  render(next);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
