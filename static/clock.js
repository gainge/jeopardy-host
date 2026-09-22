/* Cristian's algorithm against GET /api/time.
 *
 * Every timestamp the client produces is derived from one anchored Date.now()
 * reading plus performance.now() deltas. An OS clock adjustment mid-game would
 * otherwise silently poison both the offset estimate and the buzz timestamps
 * measured against it.
 */

const SAMPLES = 7;
const RESYNC_MS = 30000;

const ANCHOR_WALL = Date.now();
const ANCHOR_MONO = performance.now();

/** Local wall-clock milliseconds, immune to OS clock changes after load. */
export function localNow() {
  return ANCHOR_WALL + (performance.now() - ANCHOR_MONO);
}

export class Clock {
  constructor(onSync) {
    this.offsetMs = 0;
    this.rttMs = 0;
    this.synced = false;
    this.onSync = onSync || (() => {});
    this._timer = null;
  }

  /** Server milliseconds for a local timestamp (defaults to right now). */
  toServer(localMs) {
    return Math.round((localMs === undefined ? localNow() : localMs) + this.offsetMs);
  }

  /** Local milliseconds for a server timestamp, e.g. an unlock_at deadline. */
  toLocal(serverMs) {
    return serverMs - this.offsetMs;
  }

  serverNow() {
    return this.toServer();
  }

  start() {
    if (this._timer !== null) return;
    this.sync();
    this._timer = setInterval(() => this.sync(), RESYNC_MS);
  }

  stop() {
    if (this._timer !== null) clearInterval(this._timer);
    this._timer = null;
  }

  async _sample() {
    const sent = localNow();
    const res = await fetch('/api/time', { cache: 'no-store' });
    const recv = localNow();
    const body = await res.json();
    const rtt = recv - sent;
    return { rtt, offset: body.server_ms - (sent + rtt / 2) };
  }

  /* The minimum-RTT sample wins: a fast round trip is the one least likely to
   * have been distorted by asymmetric queueing in either direction. */
  async sync() {
    let best = null;
    for (let i = 0; i < SAMPLES; i += 1) {
      try {
        const s = await this._sample();
        if (best === null || s.rtt < best.rtt) best = s;
      } catch (err) {
        break;
      }
    }
    if (best === null) return false;
    this.offsetMs = Math.round(best.offset);
    this.rttMs = Math.round(best.rtt);
    this.synced = true;
    this.onSync(this.offsetMs, this.rttMs);
    return true;
  }
}
