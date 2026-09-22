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
  'clue_resolved', 'dd_revealed', 'dd_wager_set', 'final_category',
  'final_wagers_open', 'final_wager_received', 'final_clue',
  'final_answer_received', 'final_reveal', 'final_result', 'score_adjusted',
  'game_over', 'control_changed',
];

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 8000;

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
    if (stream) stream.close();
    stream = new EventSource(`/events?token=${encodeURIComponent(session.token)}`);
    EVENTS.forEach((type) => {
      stream.addEventListener(type, (e) => apply(type, e.data));
    });
    stream.addEventListener('open', () => {
      backoff = RECONNECT_MIN_MS;
      els.link.classList.remove('down');
      clock.sync();
    });
    stream.addEventListener('error', () => {
      els.link.classList.add('down');
      stream.close();
      setTimeout(connect, backoff);
      backoff = Math.min(RECONNECT_MAX_MS, backoff * 2);
    });
  };

  const enterGame = () => {
    els.joinView.hidden = true;
    els.gameView.hidden = false;
    clock.start();
    connect();
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
