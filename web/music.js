// Background music: a Bill Evans set played through an off-screen YouTube player.
// Browsers keep a page silent until the first click or key press, so the set starts on that gesture —
// unless the listener paused it on their last visit, which is remembered along with the volume.
// To change the set, edit TRACKS: [YouTube video id, title shown in the player].
const TRACKS = [
  ['dH3GSrCmzC8', 'Waltz for Debby'],
  ['Nv2GgV34qIg', 'Peace Piece'],
  ['a2LFVWBmoiw', 'My Foolish Heart'],
  ['r-Z8KuwI7Gc', 'Autumn Leaves'],
  ['YKRXF3b4Inc', 'Blue in Green'],
  ['adPpG0Dnxeg', 'When I Fall in Love'],
  ['PVJ5jgZU6gU', 'Young and Foolish'],
  ['ECmiiWw7rxw', 'Spring Is Here'],
  ['w2-1vwIdekA', 'Detour Ahead'],
];
const ARTIST = 'Bill Evans';
const KEY = 'hi.music';
const ICON = {
  play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z"/></svg>',
  pause: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z"/></svg>',
};
const $ = (id) => document.getElementById(id);

const prefs = { on: true, vol: 55 };
try { Object.assign(prefs, JSON.parse(localStorage.getItem(KEY))); } catch { /* first visit or storage blocked */ }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* storage blocked */ } };

// A new running order every visit.
const order = TRACKS.map((_, i) => i);
for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }

let player, at = 0, ready = false, playing = false, started = false, blocked = false, off = false, failures = 0;

const state = () => ({ off, ready, playing, wanted: prefs.on, title: TRACKS[order[at]][1], artist: ARTIST });
function render() {
  document.body.classList.toggle('playing', playing);
  $('player').hidden = off;
  $('now-title').textContent = TRACKS[order[at]][1];
  $('now-sub').textContent = !ready ? '불러오는 중…' : playing ? ARTIST : blocked && prefs.on && !started ? '아무 곳이나 누르면 재생' : prefs.on ? ARTIST : '일시정지';
  const btn = $('play'), label = playing ? '일시정지' : '재생';
  btn.innerHTML = playing ? ICON.pause : ICON.play; btn.title = label; btn.setAttribute('aria-label', label);
  $('vol').value = prefs.vol;
  document.dispatchEvent(new CustomEvent('music', { detail: state() }));
}

function toggle() {
  if (off) return;
  prefs.on = !playing; save();
  if (ready) playing ? player.pauseVideo() : player.playVideo();
  render();
}
function next() {
  if (off || !ready) return;
  at = (at + 1) % order.length;
  const id = TRACKS[order[at]][0];
  prefs.on ? player.loadVideoById(id) : player.cueVideoById(id);
  render();
}
function giveUp() { off = true; playing = false; render(); }

// The first click or key press anywhere lifts the browser's autoplay block.
function firstGesture(e) {
  if (e.target.closest?.('#player, .record')) return; // those controls decide for themselves
  if (ready && prefs.on && !playing) player.playVideo();
}
addEventListener('pointerdown', firstGesture, true);
addEventListener('keydown', firstGesture, true);

window.onYouTubeIframeAPIReady = () => {
  player = new YT.Player('yt', {
    host: 'https://www.youtube-nocookie.com',
    videoId: TRACKS[order[0]][0],
    playerVars: { autoplay: prefs.on ? 1 : 0, controls: 0, disablekb: 1, fs: 0, playsinline: 1, rel: 0, origin: location.origin },
    events: {
      onReady() {
        ready = true; player.setVolume(prefs.vol);
        if (prefs.on) player.playVideo();
        setTimeout(() => { blocked = !started; render(); }, 1500);
        render();
      },
      onStateChange({ data }) {
        if (data === YT.PlayerState.ENDED) return next();
        if (data === YT.PlayerState.BUFFERING) return;
        playing = data === YT.PlayerState.PLAYING;
        if (playing) {
          started = true; failures = 0;
          removeEventListener('pointerdown', firstGesture, true); removeEventListener('keydown', firstGesture, true);
        }
        render();
      },
      // Removed, private or embed-disabled video: move on, and stop trying once every track has failed.
      onError() { if (++failures >= order.length) giveUp(); else next(); },
    },
  });
};

$('play').onclick = toggle;
$('next').onclick = next;
$('vol').oninput = (e) => { prefs.vol = +e.target.value; save(); if (ready) player.setVolume(prefs.vol); };
window.music = { state, toggle, next };
render();

const api = document.createElement('script');
api.src = 'https://www.youtube.com/iframe_api';
api.onerror = giveUp; // offline or blocked: the site works the same, just without the music
document.head.append(api);
