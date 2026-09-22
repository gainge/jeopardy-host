/* The client's whole understanding of the game, as a pure reducer.
 *
 * Keeping this free of the DOM and of the network is what lets the UI be driven
 * from a recorded event stream (see demo.html) and what makes a `snapshot` a
 * trivial full replacement rather than a merge with a dozen edge cases.
 */

export function initialState() {
  return {
    connected: false,
    you: null,
    isHost: false,
    phase: 'lobby',
    round: null,
    players: [],
    control: null,
    board: { categories: [], cells: [] },
    clue: null,
    final: null,
    finalReveal: null,
    buzz: emptyBuzz(),
    banner: null,
    log: [],
  };
}

function emptyBuzz() {
  return { registered: [], order: [], rejected: [], mine: null };
}

function applyScores(players, scores) {
  if (!scores) return players;
  return players.map((p) => (
    Object.prototype.hasOwnProperty.call(scores, p.id) ? { ...p, score: scores[p.id] } : p
  ));
}

function bumpScore(players, playerId, delta, absolute) {
  return players.map((p) => {
    if (p.id !== playerId) return p;
    return { ...p, score: absolute === undefined ? p.score + delta : absolute };
  });
}

export function playerName(state, playerId) {
  const p = state.players.find((x) => x.id === playerId);
  return p ? p.name : (playerId || '');
}

export function reduce(state, type, data) {
  const d = data || {};
  const s = { ...state, log: state.log.concat([{ type, data: d }]).slice(-200) };

  switch (type) {
    case 'snapshot':
      return {
        ...s,
        connected: true,
        you: d.you,
        isHost: !!d.is_host,
        phase: d.phase,
        round: d.round,
        players: d.players || [],
        control: d.control,
        board: d.board || { categories: [], cells: [] },
        clue: d.clue || null,
        final: d.final || null,
        finalReveal: null,
        // Buzz race detail is per-clue and transient; a resync starts it clean.
        buzz: emptyBuzz(),
        banner: null,
      };

    case 'players':
      return { ...s, players: d.players || [] };

    case 'control_changed':
      return { ...s, control: d.player_id };

    case 'round_started':
      return {
        ...s,
        phase: 'round',
        round: d.round,
        board: { categories: d.categories || [], cells: d.cells || [] },
        clue: null,
        buzz: emptyBuzz(),
        banner: null,
      };

    case 'clue_selected':
      return {
        ...s,
        clue: {
          clue_id: d.clue_id,
          category: d.category,
          value: d.value,
          text: d.text,
          media: d.media || [],
          unlock_at: d.unlock_at,
          selected_by: d.selected_by,
          step: 'reading',
          answering: null,
          deadline: null,
          dd: null,
          answer: null,
          lastResult: null,
        },
        buzz: emptyBuzz(),
        banner: null,
      };

    case 'buzzers_open':
      if (!s.clue) return s;
      return { ...s, clue: { ...s.clue, step: 'open' } };

    case 'buzz_registered':
      if (s.buzz.registered.includes(d.player_id)) return s;
      return { ...s, buzz: { ...s.buzz, registered: s.buzz.registered.concat([d.player_id]) } };

    case 'buzz_result':
      return {
        ...s,
        clue: s.clue ? { ...s.clue, step: 'window' } : s.clue,
        buzz: { ...s.buzz, order: d.order || [], rejected: d.rejected || [] },
      };

    case 'answering':
      if (!s.clue) return s;
      return {
        ...s,
        clue: {
          ...s.clue,
          step: s.clue.dd ? 'dd_answering' : 'answering',
          answering: d.player_id,
          deadline: d.deadline,
          lastResult: null,
        },
      };

    case 'answer_result':
      return {
        ...s,
        players: bumpScore(s.players, d.player_id, d.score_delta),
        clue: s.clue ? {
          ...s.clue,
          answering: null,
          deadline: null,
          lastResult: { player_id: d.player_id, correct: !!d.correct, score_delta: d.score_delta },
        } : s.clue,
      };

    case 'clue_resolved':
      return {
        ...s,
        players: applyScores(s.players, d.scores),
        clue: s.clue ? {
          ...s.clue, step: 'resolved', answer: d.answer, answering: null, deadline: null,
        } : s.clue,
      };

    case 'dd_revealed':
      if (!s.clue) return s;
      return {
        ...s,
        clue: {
          ...s.clue,
          step: 'dd_wager',
          dd: {
            player_id: d.player_id,
            min_wager: d.min_wager,
            max_wager: d.max_wager,
            amount: null,
          },
        },
      };

    case 'dd_wager_set':
      if (!s.clue || !s.clue.dd) return s;
      return { ...s, clue: { ...s.clue, dd: { ...s.clue.dd, amount: d.amount } } };

    case 'final_category':
      return {
        ...s,
        phase: 'final',
        round: 'FJ',
        clue: null,
        control: null,
        banner: null,
        final: {
          step: 'category',
          category: d.category,
          participants: [],
          submitted: [],
          answered: [],
          you_wagered: false,
          you_answered: false,
          text: null,
          media: [],
          deadline: null,
        },
      };

    case 'final_wagers_open':
      return {
        ...s,
        final: {
          ...(s.final || {}),
          step: 'wagers',
          participants: d.participants || [],
          deadline: d.deadline,
        },
      };

    case 'final_wager_received':
      if (!s.final) return s;
      return {
        ...s,
        final: {
          ...s.final,
          submitted: s.final.submitted.includes(d.player_id)
            ? s.final.submitted : s.final.submitted.concat([d.player_id]),
          you_wagered: d.player_id === s.you ? true : s.final.you_wagered,
        },
      };

    case 'final_clue':
      return {
        ...s,
        final: {
          ...(s.final || {}),
          step: 'clue',
          text: d.text,
          media: d.media || [],
          deadline: d.deadline,
        },
      };

    case 'final_answer_received':
      if (!s.final) return s;
      return {
        ...s,
        final: {
          ...s.final,
          answered: (s.final.answered || []).includes(d.player_id)
            ? s.final.answered : (s.final.answered || []).concat([d.player_id]),
          you_answered: d.player_id === s.you ? true : s.final.you_answered,
        },
      };

    case 'final_reveal':
      return {
        ...s,
        final: { ...(s.final || {}), step: 'reveal' },
        finalReveal: { answer: d.answer, entries: d.entries || [], results: [] },
      };

    case 'final_result':
      return {
        ...s,
        players: applyScores(s.players, d.scores),
        finalReveal: s.finalReveal ? {
          ...s.finalReveal,
          results: s.finalReveal.results.concat([{
            player_id: d.player_id, correct: !!d.correct, score_delta: d.score_delta,
          }]),
        } : s.finalReveal,
      };

    case 'score_adjusted':
      return {
        ...s,
        players: bumpScore(s.players, d.player_id, d.delta, d.score),
        banner: `${d.delta >= 0 ? '+' : ''}${d.delta} — ${d.reason}`,
      };

    case 'game_over':
      return {
        ...s,
        phase: 'game_over',
        players: applyScores(s.players, d.scores),
        winner: d.winner,
      };

    case 'error':
      return { ...s, banner: d.message };

    default:
      return s;
  }
}
