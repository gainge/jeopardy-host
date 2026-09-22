/* All non-host rendering: the grid, the clue takeover, Final, and the score strip.
 *
 * Rendering is a full redraw from state on every event. A game board is a few
 * dozen nodes, and a redraw cannot drift out of sync with a snapshot the way an
 * incremental patcher can.
 */

import { playerName } from './state.js';

const BOARD_ROWS = 5;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function money(n) {
  return `$${Math.abs(n).toLocaleString('en-US')}`;
}

function signedMoney(n) {
  return `${n < 0 ? '-' : ''}${money(n)}`;
}

export function render(state, ctx) {
  const { els } = ctx;
  els.banner.textContent = state.banner || '';
  els.banner.hidden = !state.banner;

  renderTopbar(state, els);
  renderScores(state, els);

  const inFinal = state.phase === 'final' || state.phase === 'game_over';
  els.final.hidden = !inFinal;
  els.clue.hidden = inFinal || !state.clue;
  els.board.hidden = inFinal || !!state.clue;

  if (inFinal) renderFinal(state, ctx);
  else if (state.clue) renderClue(state, ctx);
  else renderBoard(state, ctx);
}

function renderTopbar(state, els) {
  const roundName = { J: 'Jeopardy!', DJ: 'Double Jeopardy!', FJ: 'Final Jeopardy!' };
  els.round.textContent = roundName[state.round] || 'Lobby';
  const controller = state.control ? playerName(state, state.control) : null;
  els.control.textContent = controller
    ? (state.control === state.you ? 'your board' : `${controller}'s board`)
    : '';
}

function renderBoard(state, ctx) {
  const { els } = ctx;
  const grid = els.board;
  grid.replaceChildren();
  const cats = state.board.categories || [];
  const cells = state.board.cells || [];
  const yours = state.control === state.you;

  cats.forEach((name) => grid.append(el('div', 'cat', name)));

  // col and row are 1-indexed on the wire, matching j-archive's own clue ids.
  for (let row = 1; row <= BOARD_ROWS; row += 1) {
    for (let col = 1; col <= cats.length; col += 1) {
      const cell = cells.find((c) => c.col === col && c.row === row);
      if (!cell) {
        grid.append(el('div', 'cell spent'));
        continue;
      }
      if (!cell.available) {
        grid.append(el('div', 'cell spent'));
        continue;
      }
      const btn = el('button', 'cell', money(cell.value));
      btn.disabled = !yours;
      btn.addEventListener('click', () => ctx.onSelectClue(cell.clue_id));
      grid.append(btn);
    }
  }
}

function renderMedia(container, media) {
  container.replaceChildren();
  (media || []).forEach((src) => {
    const lower = src.toLowerCase();
    if (lower.endsWith('.mp3') || lower.endsWith('.wav') || lower.endsWith('.ogg')) {
      const audio = document.createElement('audio');
      audio.controls = true;
      audio.src = src;
      container.append(audio);
    } else {
      const img = document.createElement('img');
      // j-archive's /media/ assets are all 404 now, and a broken-image icon
      // sitting over the clue reads as a bug. Drop what will not load.
      img.addEventListener('error', () => img.remove());
      img.src = src;
      img.alt = 'clue media';
      container.append(img);
    }
  });
}

function renderClue(state, ctx) {
  const { els } = ctx;
  const clue = state.clue;
  els.clueCategory.textContent = clue.category;
  els.clueValue.textContent = clue.dd ? 'DAILY DOUBLE' : money(clue.value);
  els.clueValue.classList.toggle('dd', !!clue.dd);
  els.clueText.textContent = clue.text;
  renderMedia(els.clueMedia, clue.media);

  els.clueAnswer.hidden = !clue.answer;
  els.clueAnswer.textContent = clue.answer ? clue.answer : '';

  const dd = clue.dd;
  const youWager = !!dd && dd.player_id === state.you && dd.amount === null;
  els.ddForm.hidden = !youWager;
  if (youWager) {
    els.ddInput.min = String(dd.min_wager);
    els.ddInput.max = String(dd.max_wager);
    els.ddRange.textContent = `${money(dd.min_wager)} – ${money(dd.max_wager)}`;
  }

  const answering = clue.answering;
  const yourTurn = answering && answering === state.you;
  els.selfReport.hidden = !yourTurn;

  els.clueStatus.textContent = clueStatus(state, clue);
}

function clueStatus(state, clue) {
  if (clue.step === 'resolved') return 'correct response:';
  if (clue.answering) {
    return clue.answering === state.you
      ? 'you have the buzz — say it out loud, then report'
      : `${playerName(state, clue.answering)} is answering`;
  }
  if (clue.dd && clue.dd.amount === null) {
    return `Daily Double — ${playerName(state, clue.dd.player_id)} is wagering`;
  }
  if (clue.dd) return `wager ${money(clue.dd.amount)}`;
  if (clue.step === 'reading') return 'buzzers locked';
  if (clue.step === 'window') return 'resolving buzz order';
  return 'buzzers open';
}

function renderScores(state, els) {
  els.scores.replaceChildren();
  const sorted = state.players.slice().sort((a, b) => b.score - a.score);
  sorted.forEach((p) => {
    const card = el('div', 'score-card');
    if (p.id === state.you) card.classList.add('you');
    if (state.control === p.id) card.classList.add('control');
    if (!p.connected) card.classList.add('offline');
    if (state.clue && state.clue.answering === p.id) card.classList.add('answering');
    if ((state.buzz.registered || []).includes(p.id)) card.classList.add('buzzed');

    const name = el('div', 'score-name', p.name + (p.is_host ? ' •' : ''));
    const val = el('div', 'score-value', signedMoney(p.score));
    if (p.score < 0) val.classList.add('negative');
    card.append(name, val);
    els.scores.append(card);
  });
}

function renderFinal(state, ctx) {
  const { els } = ctx;
  const f = state.final || {};
  els.finalCategory.textContent = f.category || '';
  els.finalText.textContent = f.text || '';
  renderMedia(els.finalMedia, f.media);

  const wagering = f.step === 'wagers' && !f.you_wagered
    && (f.participants || []).includes(state.you);
  els.finalWagerForm.hidden = !wagering;

  const answering = f.step === 'clue' && !f.you_answered
    && (f.participants || []).includes(state.you);
  els.finalAnswerForm.hidden = !answering;

  els.finalStatus.textContent = finalStatus(state, f);

  els.finalReveal.replaceChildren();
  els.finalSelfReport.hidden = true;
  if (state.finalReveal) {
    const head = el('div', 'reveal-answer', state.finalReveal.answer);
    els.finalReveal.append(head);
    state.finalReveal.entries.forEach((entry) => {
      const row = el('div', 'reveal-row');
      row.append(el('span', 'reveal-name', playerName(state, entry.player_id)));
      row.append(el('span', 'reveal-said', entry.answer || '—'));
      row.append(el('span', 'reveal-wager', money(entry.wager)));
      const result = state.finalReveal.results.find((r) => r.player_id === entry.player_id);
      if (result) {
        row.classList.add(result.correct ? 'right' : 'wrong');
        row.append(el('span', 'reveal-delta', signedMoney(result.score_delta)));
      } else if (entry.player_id === state.you && !state.finalReported) {
        els.finalSelfReport.hidden = false;
      }
      els.finalReveal.append(row);
    });
  }
  if (!state.finalReveal || state.phase === 'game_over') els.finalSelfReport.hidden = true;

  if (state.phase === 'game_over' && state.winner) {
    els.finalStatus.textContent = `${playerName(state, state.winner)} wins.`;
  }
}

function finalStatus(state, f) {
  const submitted = (f.submitted || []).length;
  const total = (f.participants || []).length;
  switch (f.step) {
    case 'category': return 'category revealed — wagers next';
    case 'wagers': return `wagers in: ${submitted}/${total}`;
    case 'clue': return `answers in: ${(f.answered || []).length}/${total}`;
    case 'reveal': return 'correct response:';
    default: return '';
  }
}
