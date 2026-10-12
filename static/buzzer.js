/* The buzzer: local unlock timing, same-frame optimistic feedback, race readout.
 *
 * Unlock is computed in *local* time from the server's unlock_at, so two clients
 * 80ms apart on the wire still arm at the same real instant. An early press is
 * simply ignored with a hint; a lockout penalty would punish exactly the players
 * whose clock estimate is worst, which is the opposite of the goal here.
 */

import { localNow } from './clock.js';
import { buzzLockedOut } from './state.js';

const READY = 'ready';
const LOCKED = 'locked';
const PRESSED = 'pressed';
const EARLY = 'early';
const CLOSED = 'closed';
const IDLE = 'idle';

const EARLY_HINT_MS = 700;

export class Buzzer {
  constructor({ clock, send, button, hint, readout }) {
    this.clock = clock;
    this.send = send;
    this.button = button;
    this.hint = hint;
    this.readout = readout;
    this.clueId = null;
    this.unlockLocal = null;
    this.mode = IDLE;
    this.pressServer = null;
    this._earlyTimer = null;
    this._raf = null;

    this.button.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      this.press();
    });
    window.addEventListener('keydown', (e) => {
      if (e.code !== 'Space' && e.key !== ' ') return;
      if (e.repeat) return;
      const tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      e.preventDefault();
      this.press();
    });
  }

  /* The POST is fired before anything else touches the DOM, and nothing in this
   * method awaits: the press timestamp and the visible feedback both land in the
   * frame of the physical press, not a round trip later. */
  press() {
    if (this.clueId === null || this.mode === PRESSED || this.mode === CLOSED) return;
    const pressLocal = localNow();
    if (this.unlockLocal !== null && pressLocal < this.unlockLocal) {
      this._flashEarly();
      return;
    }
    const pressServer = this.clock.toServer(pressLocal);
    this.send({
      type: 'buzz',
      clue_id: this.clueId,
      press_server: pressServer,
      offset_ms: Math.round(this.clock.offsetMs),
      rtt_ms: Math.round(this.clock.rttMs),
    });
    this.pressServer = pressServer;
    this.mode = PRESSED;
    this._paint();
  }

  /** Called on every state change; owns arming and disarming. */
  sync(state) {
    const clue = state.clue;
    if (!clue || clue.step === 'resolved' || clue.dd) {
      this.clueId = null;
      this.unlockLocal = null;
      this.mode = IDLE;
      this._paint();
      this._renderReadout(state);
      return;
    }
    if (clue.clue_id !== this.clueId) {
      this.clueId = clue.clue_id;
      this.pressServer = null;
      this.mode = LOCKED;
      this.unlockLocal = clue.unlock_at ? this.clock.toLocal(clue.unlock_at) : localNow();
      this._tick();
    }
    if (clue.answering) {
      this.mode = clue.answering === state.you ? PRESSED : CLOSED;
      this._paint();
    } else if (buzzLockedOut(state)) {
      this.mode = CLOSED;
      this._paint();
    } else if (clue.step === 'open' && this.mode === CLOSED) {
      // A miss reopened the clue to everyone who has not tried it yet. PRESSED is
      // left alone: the step reads 'open' until buzz_result, so a fresh press
      // would otherwise re-arm itself.
      this.pressServer = null;
      this.mode = LOCKED;
      this._tick();
    } else if (this.mode === LOCKED || this.mode === READY) {
      this._tick();
    } else {
      this._paint();
    }
    this._renderReadout(state);
  }

  _tick() {
    if (this._raf !== null) cancelAnimationFrame(this._raf);
    if (this.clueId === null || this.mode === PRESSED) return;
    const remaining = this.unlockLocal - localNow();
    this.mode = remaining > 0 ? LOCKED : READY;
    this._paint(Math.max(0, remaining));
    if (remaining > 0) this._raf = requestAnimationFrame(() => this._tick());
  }

  _flashEarly() {
    this.mode = EARLY;
    this._paint();
    if (this._earlyTimer !== null) clearTimeout(this._earlyTimer);
    this._earlyTimer = setTimeout(() => {
      if (this.mode === EARLY) this.mode = LOCKED;
      this._tick();
    }, EARLY_HINT_MS);
  }

  _paint(remainingMs) {
    const b = this.button;
    b.dataset.mode = this.mode;
    b.disabled = this.mode === IDLE || this.mode === CLOSED;
    if (this.mode === IDLE) {
      b.textContent = 'BUZZER';
      this.hint.textContent = '';
    } else if (this.mode === LOCKED) {
      const secs = remainingMs === undefined ? 0 : remainingMs / 1000;
      b.textContent = 'WAIT';
      this.hint.textContent = `opens in ${secs.toFixed(1)}s`;
    } else if (this.mode === READY) {
      b.textContent = 'BUZZ';
      this.hint.textContent = 'space or click';
    } else if (this.mode === PRESSED) {
      b.textContent = 'IN';
      this.hint.textContent = 'buzz sent';
    } else if (this.mode === EARLY) {
      b.textContent = 'TOO EARLY';
      this.hint.textContent = 'press again — no penalty';
    } else {
      b.textContent = 'LOCKED';
      this.hint.textContent = '';
    }
  }

  _renderReadout(state) {
    const order = state.buzz.order || [];
    const rejected = state.buzz.rejected || [];
    const registered = state.buzz.registered || [];
    this.readout.replaceChildren();
    if (!order.length && !rejected.length && !registered.length) return;

    const name = (id) => {
      const p = state.players.find((x) => x.id === id);
      return p ? p.name : id;
    };
    const row = (label, detail, cls) => {
      const li = document.createElement('li');
      li.className = cls;
      const n = document.createElement('span');
      n.className = 'race-name';
      n.textContent = label;
      const d = document.createElement('span');
      d.className = 'race-delta';
      d.textContent = detail;
      li.append(n, d);
      this.readout.append(li);
    };

    if (!order.length) {
      registered.forEach((id) => row(name(id), 'in', 'race-pending'));
      return;
    }
    order.forEach((b, i) => {
      const detail = i === 0 ? 'first' : `+${b.delta_ms} ms`;
      row(name(b.player_id), b.status === 'accepted' ? detail : `${detail} · ${b.status}`,
        i === 0 ? 'race-first' : 'race-row');
    });
    rejected.forEach((b) => row(name(b.player_id), b.status, 'race-rejected'));
  }
}
