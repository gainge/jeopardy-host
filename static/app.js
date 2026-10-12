/* Wiring: join, session persistence, the SSE stream, and the render loop.
 *
 * The session token lives in localStorage so a mid-game refresh costs nothing:
 * reconnecting replays a `snapshot`, which replaces state wholesale.
 */

import { Clock } from './clock.js';
import { initialState, reduce } from './state.js';
import { render } from './board.js';
import { Buzzer } from './buzzer.js';
import { mountHost, renderHost } from './host.js';

const SESSION_KEY = 'jeopardy.session';

const EVENTS = [
  'snapshot', 'error', 'players', 'round_started', 'clue_selected',
  'buzzers_open', 'buzz_registered', 'buzz_result', 'answering',
  'answer_revealed', 'answer_disclosed', 'answer_result',
  'clue_resolved', 'clue_dismissed', 'dd_revealed', 'dd_wager_set', 'final_category',
  'final_wagers_open', 'final_wager_received', 'final_clue',
  'final_answer_received', 'final_reveal', 'final_result', 'score_adjusted',
  'game_over', 'control_changed',
];

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 8000;

// The server pings every 10s. A stream silent for longer than two of those has
// died without the socket noticing (sleep, VPN or NAT drop), and EventSource
// would otherwise sit in OPEN forever showing a frozen game.
const PING_MS = 10000;
const STALE_MS = 25000;
const WATCHDOG_MS = 5000;

function ids(names) {
  const out = {};
  names.forEach(([key, id]) => { out[key] = document.getElementById(id); });
  return out;
}

export function collectEls() {
  return ids([
    ['joinView', 'join-view'], ['joinForm', 'join-form'], ['joinName', 'join-name'],
    ['joinKey', 'join-key'], ['joinError', 'join-error'],
    ['gameView', 'game-view'], ['banner', 'banner'],
    ['round', 'round-label'], ['control', 'control-label'], ['link', 'link-state'],
    ['board', 'board'],
    ['clue', 'clue-view'], ['clueCategory', 'clue-category'], ['clueValue', 'clue-value'],
    ['clueText', 'clue-text'], ['clueMedia', 'clue-media'], ['clueStatus', 'clue-status'],
    ['clueAnswer', 'clue-answer'],
    ['selfReport', 'self-report'], ['answerSpoiler', 'answer-spoiler'],
    ['reportRight', 'report-right'], ['reportWrong', 'report-wrong'],
    ['ddForm', 'dd-form'], ['ddInput', 'dd-input'], ['ddRange', 'dd-range'],
    ['final', 'final-view'], ['finalCategory', 'final-category'],
    ['finalText', 'final-text'], ['finalMedia', 'final-media'],
    ['finalStatus', 'final-status'], ['finalReveal', 'final-reveal'],
    ['finalWagerForm', 'final-wager-form'], ['finalWagerInput', 'final-wager-input'],
    ['finalAnswerForm', 'final-response-form'],
    ['finalAnswerInput', 'final-response-input'],
    ['finalSelfReport', 'final-self-report'],
    ['finalRight', 'final-right'], ['finalWrong', 'final-wrong'],
    ['buzzButton', 'buzz-button'], ['buzzHint', 'buzz-hint'], ['race', 'race-readout'],
    ['scores', 'scores'],
    ['hostPanel', 'host-panel'], ['hostStart', 'host-start'], ['hostNext', 'host-next'],
    ['hostResolve', 'host-resolve'], ['hostReveal', 'host-reveal'],
    ['hostReturn', 'host-return'],
    ['hostUndo', 'host-undo'],
    ['hostTarget', 'host-target'], ['hostControl', 'host-control'],
    ['hostKick', 'host-kick'], ['hostAdjustForm', 'host-adjust-form'],
    ['hostAdjustAmount', 'host-adjust-amount'],
    ['hostAdjustReason', 'host-adjust-reason'], ['hostNegate', 'host-negate'],
    ['hostDiag', 'host-diag'], ['hostFlags', 'host-flags'],
  ]);
}

function loadSession() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
  } catch (err) {
    return null;
  }
}

function saveSession(session) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch (err) {
    /* A locked-down browser profile costs reconnect convenience, nothing more. */
  }
}

export function main() {
  const els = collectEls();
  let state = initialState();
  let session = loadSession();
  let stream = null;
  let backoff = RECONNECT_MIN_MS;
  let lastFrameAt = 0;
  let retryTimer = null;

  const send = (command) => {
    if (!session) return;
    fetch(`/api/command?token=${encodeURIComponent(session.token)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(command),
    }).catch(() => {});
  };

  const clock = new Clock((offsetMs, rttMs) => {
    send({ type: 'set_clock', offset_ms: offsetMs, rtt_ms: rttMs });
    els.link.textContent = `${rttMs} ms`;
  });

  const buzzer = new Buzzer({
    clock, send, button: els.buzzButton, hint: els.buzzHint, readout: els.race,
  });

  const ctx = { els, onSelectClue: (id) => send({ type: 'select_clue', clue_id: id }) };

  const paint = () => {
    render(state, ctx);
    renderHost(state, els);
    buzzer.sync(state);
  };

  const apply = (type, raw) => {
    let data = {};
    try {
      data = JSON.parse(raw);
    } catch (err) {
      return;
    }
    state = reduce(state, type, data);
    paint();
  };

  const connect = () => {
    if (retryTimer !== null) clearTimeout(retryTimer);
    retryTimer = null;
    if (stream) stream.close();
    lastFrameAt = Date.now();
    const es = new EventSource(`/events?token=${encodeURIComponent(session.token)}`);
    stream = es;
    const alive = () => { lastFrameAt = Date.now(); };
    EVENTS.forEach((type) => {
      es.addEventListener(type, (e) => { alive(); apply(type, e.data); });
    });
    es.addEventListener('ping', alive);
    es.addEventListener('open', () => {
      alive();
      backoff = RECONNECT_MIN_MS;
      els.link.classList.remove('down');
      clock.sync();
    });
    es.addEventListener('error', () => {
      // A superseded stream's late error must not schedule a second reconnect.
      if (es !== stream) return;
      els.link.classList.add('down');
      es.close();
      retryTimer = setTimeout(connect, backoff);
      backoff = Math.min(RECONNECT_MAX_MS, backoff * 2);
    });
  };

  // Reconnecting is always safe: the server opens every stream with a snapshot
  // that replaces client state wholesale.
  const resyncIfStale = (thresholdMs) => {
    if (!stream || retryTimer !== null) return;
    if (Date.now() - lastFrameAt > thresholdMs) {
      els.link.classList.add('down');
      connect();
    }
  };

  let watching = false;
  const watchLiveness = () => {
    if (watching) return;
    watching = true;
    setInterval(() => resyncIfStale(STALE_MS), WATCHDOG_MS);
    // Timers are throttled in background tabs and frozen across sleep, so
    // coming back is checked directly rather than left to the next interval.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') resyncIfStale(PING_MS);
    });
    window.addEventListener('online', () => resyncIfStale(0));
    window.addEventListener('pageshow', (e) => {
      if (e.persisted) resyncIfStale(0);
    });
  };

  const enterGame = () => {
    els.joinView.hidden = true;
    els.gameView.hidden = false;
    clock.start();
    connect();
    watchLiveness();
    paint();
  };

  els.joinForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { name: els.joinName.value.trim() };
    if (els.joinKey.value.trim()) body.admin_token = els.joinKey.value.trim();
    if (!body.name) return;
    try {
      const res = await fetch('/api/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok || !data.token) throw new Error(data.message || 'join refused');
      session = { player_id: data.player_id, token: data.token, name: body.name };
      saveSession(session);
      enterGame();
    } catch (err) {
      els.joinError.textContent = String(err.message || err);
      els.joinError.hidden = false;
    }
  });

  els.answerSpoiler.addEventListener('click', () => {
    els.answerSpoiler.classList.toggle('shown');
  });

  els.reportRight.addEventListener('click', () => report(true));
  els.reportWrong.addEventListener('click', () => report(false));

  function report(correct) {
    if (!state.clue) return;
    send({ type: 'self_report', clue_id: state.clue.clue_id, correct });
    els.selfReport.hidden = true;
  }

  els.ddForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const amount = parseInt(els.ddInput.value, 10);
    if (Number.isNaN(amount)) return;
    send({ type: 'dd_wager', amount });
    els.ddForm.hidden = true;
  });

  els.finalWagerForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const amount = parseInt(els.finalWagerInput.value, 10);
    if (Number.isNaN(amount)) return;
    send({ type: 'final_wager', amount });
    els.finalWagerForm.hidden = true;
  });

  els.finalAnswerForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = els.finalAnswerInput.value.trim();
    if (!text) return;
    send({ type: 'final_answer', text });
    els.finalAnswerForm.hidden = true;
  });

  const finalReport = (correct) => {
    send({ type: 'final_report', correct });
    state = { ...state, finalReported: true };
    paint();
  };
  els.finalRight.addEventListener('click', () => finalReport(true));
  els.finalWrong.addEventListener('click', () => finalReport(false));

  mountHost(els, send);

  if (session && session.token) {
    els.joinName.value = session.name || '';
    enterGame();
  } else {
    paint();
  }
}
