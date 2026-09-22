/* Drives the real renderers from the recorded stream, with no server present.
 *
 * The markup is lifted out of index.html rather than copied, so the demo cannot
 * drift into testing a layout the actual game does not ship.
 */

import { collectEls } from './app.js';
import { initialState, reduce } from './state.js';
import { render } from './board.js';
import { renderHost } from './host.js';
import { Buzzer } from './buzzer.js';
import { RECORDED_STREAM } from './fixtures/demo_stream.js';

const PLAY_MS = 900;

const stubClock = {
  offsetMs: 0,
  rttMs: 24,
  toServer: (localMs) => Math.round(localMs === undefined ? Date.now() : localMs),
  toLocal: (serverMs) => Date.now() + 3000,
};

async function mount() {
  const res = await fetch('./index.html', { cache: 'no-store' });
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  const view = doc.getElementById('game-view');
  view.hidden = false;
  document.getElementById('demo-mount').append(document.importNode(view, true));
}

mount().then(() => {
  const els = collectEls();
  const buzzer = new Buzzer({
    clock: stubClock,
    send: (cmd) => console.log('command', cmd),
    button: els.buzzButton,
    hint: els.buzzHint,
    readout: els.race,
  });
  const ctx = { els, onSelectClue: (id) => console.log('select_clue', id) };

  let state = initialState();
  let cursor = 0;
  let timer = null;
  const label = document.getElementById('demo-label');

  const paint = () => {
    render(state, ctx);
    renderHost(state, els);
    buzzer.sync(state);
    const last = cursor > 0 ? RECORDED_STREAM[cursor - 1][0] : 'start';
    label.textContent = `${cursor}/${RECORDED_STREAM.length} — ${last}`;
  };

  const step = () => {
    if (cursor >= RECORDED_STREAM.length) {
      if (timer) { clearInterval(timer); timer = null; }
      return;
    }
    const [type, data] = RECORDED_STREAM[cursor];
    cursor += 1;
    state = reduce(state, type, data);
    paint();
  };

  document.getElementById('demo-step').addEventListener('click', step);
  document.getElementById('demo-play').addEventListener('click', () => {
    if (timer) { clearInterval(timer); timer = null; return; }
    timer = setInterval(step, PLAY_MS);
  });
  document.getElementById('demo-reset').addEventListener('click', () => {
    if (timer) { clearInterval(timer); timer = null; }
    state = initialState();
    cursor = 0;
    paint();
  });

  paint();
});
