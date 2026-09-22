/* A recorded-shape event stream used by demo.html and by the client gate test.
 *
 * It exists because Phases 2 and 3 land in parallel with this one: the client has
 * to be drivable, and assertable, without a live server. Every payload here is
 * written straight off docs/PROTOCOL.md.
 */

const YOU = 'p1';

const PLAYERS = [
  { id: 'p1', name: 'Gibson', score: 0, connected: true, is_host: true, rtt_ms: 12, offset_ms: -3 },
  { id: 'p2', name: 'Dana', score: 0, connected: true, is_host: false, rtt_ms: 68, offset_ms: 240 },
  { id: 'p3', name: 'Ravi', score: 0, connected: true, is_host: false, rtt_ms: 210, offset_ms: -1180 },
];

const CATEGORIES = [
  'POTENT POTABLES', 'STATE CAPITALS', 'WORLD LITERATURE',
  '19TH CENTURY SCIENCE', 'RHYME TIME', 'THE OFFICE PARK',
];

function cells(round, taken) {
  const values = round === 'DJ' ? [400, 800, 1200, 1600, 2000] : [200, 400, 600, 800, 1000];
  const out = [];
  // 1-indexed, matching the server and j-archive's clue ids. A 0-indexed fixture
  // will agree with itself and disagree with every real board.
  for (let col = 1; col <= 6; col += 1) {
    for (let row = 1; row <= 5; row += 1) {
      const id = `${round}_${col}_${row}`;
      out.push({
        clue_id: id, col, row, value: values[row - 1], available: !taken.includes(id),
      });
    }
  }
  return out;
}

const T0 = 1730000000000;

export const RECORDED_STREAM = [
  ['snapshot', {
    phase: 'lobby', round: null, you: YOU, is_host: true, players: PLAYERS,
    control: null, board: { categories: [], cells: [] }, clue: null, final: null,
  }],
  ['round_started', { round: 'J', categories: CATEGORIES, cells: cells('J', []) }],
  ['control_changed', { player_id: 'p2' }],

  ['clue_selected', {
    clue_id: 'J_1_2', category: 'STATE CAPITALS', value: 600,
    text: 'This capital sits at the confluence of the Missouri and the Kansas rivers',
    media: [], unlock_at: T0 + 5200, selected_by: 'p2',
  }],
  ['buzzers_open', { clue_id: 'J_1_2' }],
  ['buzz_registered', { clue_id: 'J_1_2', player_id: 'p3' }],
  ['buzz_registered', { clue_id: 'J_1_2', player_id: 'p1' }],
  ['buzz_result', {
    clue_id: 'J_1_2',
    order: [
      { player_id: 'p1', delta_ms: 0, status: 'accepted', press_server: T0 + 5410 },
      { player_id: 'p3', delta_ms: 84, status: 'accepted', press_server: T0 + 5494 },
    ],
    rejected: [{ player_id: 'p2', status: 'early' }],
  }],
  ['answering', { clue_id: 'J_1_2', player_id: 'p1', deadline: T0 + 17410 }],
  ['answer_result', { clue_id: 'J_1_2', player_id: 'p1', correct: false, score_delta: -600 }],
  ['answering', { clue_id: 'J_1_2', player_id: 'p3', deadline: T0 + 30000 }],
  ['answer_result', { clue_id: 'J_1_2', player_id: 'p3', correct: true, score_delta: 600 }],
  ['clue_resolved', {
    clue_id: 'J_1_2', answer: 'Topeka', scores: { p1: -600, p2: 0, p3: 600 },
  }],

  ['control_changed', { player_id: 'p3' }],
  ['clue_selected', {
    clue_id: 'J_4_3', category: 'RHYME TIME', value: 800,
    text: 'A stationary hurt: the pain that will not travel',
    media: [], unlock_at: T0 + 40000, selected_by: 'p3',
  }],
  ['dd_revealed', { clue_id: 'J_4_3', player_id: 'p3', min_wager: 5, max_wager: 1000 }],
  ['dd_wager_set', { clue_id: 'J_4_3', player_id: 'p3', amount: 600 }],
  ['answering', { clue_id: 'J_4_3', player_id: 'p3', deadline: T0 + 52000 }],
  ['answer_result', { clue_id: 'J_4_3', player_id: 'p3', correct: true, score_delta: 600 }],
  ['clue_resolved', {
    clue_id: 'J_4_3', answer: 'a fixed affliction', scores: { p1: -600, p2: 0, p3: 1200 },
  }],

  ['score_adjusted', {
    player_id: 'p1', delta: 600, reason: 'ruled acceptable on the call',
    by: 'p1', score: 0,
  }],

  ['final_category', { category: 'EXPLORERS' }],
  ['final_wagers_open', { participants: ['p1', 'p3'], deadline: T0 + 100000 }],
  ['final_wager_received', { player_id: 'p3' }],
  ['final_wager_received', { player_id: 'p1' }],
  ['final_clue', {
    text: 'In 1911 this Norwegian beat Scott to the South Pole by 34 days',
    media: [], deadline: T0 + 145000,
  }],
  ['final_answer_received', { player_id: 'p1' }],
  ['final_answer_received', { player_id: 'p3' }],
  ['final_reveal', {
    answer: 'Roald Amundsen',
    entries: [
      { player_id: 'p1', wager: 0, answer: 'Who is Amundsen?' },
      { player_id: 'p3', wager: 400, answer: 'Who is Shackleton?' },
    ],
  }],
  ['final_result', {
    player_id: 'p1', correct: true, score_delta: 0, scores: { p1: 0, p2: 0, p3: 1200 },
  }],
  ['final_result', {
    player_id: 'p3', correct: false, score_delta: -400, scores: { p1: 0, p2: 0, p3: 800 },
  }],
  ['game_over', { scores: { p1: 0, p2: 0, p3: 800 }, winner: 'p3' }],
];

export const DEMO_YOU = YOU;
