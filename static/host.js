/* The facilitator panel.
 *
 * The host plays, so this module deliberately contains no clue disclosure of any
 * kind — the server never sends the host one, and a panel that looked like it
 * might would spoil the person running the call. Everything here is procedural:
 * round control, score correction, roster management, and clock diagnostics.
 */

import { playerName } from './state.js';

const FLAGGED = new Set(['early', 'future', 'clamped', 'duplicate']);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function mountHost(els, send) {
  els.hostStart.addEventListener('click', () => send({ type: 'host_start_round' }));
  els.hostNext.addEventListener('click', () => send({ type: 'host_next_round' }));
  els.hostResolve.addEventListener('click', () => send({ type: 'host_force_resolve' }));
  els.hostUndo.addEventListener('click', () => send({ type: 'host_undo' }));

  els.hostAdjustForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const delta = parseInt(els.hostAdjustAmount.value, 10);
    const target = els.hostTarget.value;
    if (!target || Number.isNaN(delta) || delta === 0) return;
    send({
      type: 'host_adjust_score',
      player_id: target,
      delta,
      reason: els.hostAdjustReason.value.trim() || 'host correction',
    });
    els.hostAdjustReason.value = '';
  });

  els.hostNegate.addEventListener('click', () => {
    els.hostAdjustAmount.value = String(-parseInt(els.hostAdjustAmount.value || '0', 10));
  });

  els.hostControl.addEventListener('click', () => {
    const target = els.hostTarget.value;
    if (target) send({ type: 'host_set_control', player_id: target });
  });

  els.hostKick.addEventListener('click', () => {
    const target = els.hostTarget.value;
    if (target && window.confirm(`Remove ${target} from the game?`)) {
      send({ type: 'host_kick', player_id: target });
    }
  });
}

export function renderHost(state, els) {
  els.hostPanel.hidden = !state.isHost;
  if (!state.isHost) return;

  const previous = els.hostTarget.value;
  els.hostTarget.replaceChildren();
  state.players.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    els.hostTarget.append(opt);
  });
  if (previous && state.players.some((p) => p.id === previous)) {
    els.hostTarget.value = previous;
  }

  els.hostStart.hidden = state.phase !== 'lobby';
  els.hostNext.hidden = state.phase === 'lobby';

  els.hostDiag.replaceChildren();
  state.players.forEach((p) => {
    const row = el('div', 'diag-row');
    row.append(el('span', 'diag-name', p.name));
    row.append(el('span', 'diag-rtt', `${p.rtt_ms} ms rtt`));
    row.append(el('span', 'diag-offset', `${p.offset_ms >= 0 ? '+' : ''}${p.offset_ms} ms`));
    if (!p.connected) row.classList.add('offline');
    if (p.rtt_ms > 250) row.classList.add('slow');
    els.hostDiag.append(row);
  });

  els.hostFlags.replaceChildren();
  const suspect = (state.buzz.order || []).concat(state.buzz.rejected || [])
    .filter((b) => FLAGGED.has(b.status));
  suspect.forEach((b) => {
    els.hostFlags.append(el('div', 'flag-row',
      `${playerName(state, b.player_id)}: ${b.status}`));
  });
}
