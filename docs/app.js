// === app.js — 폰 단독 영상 반복 플레이어 ===
// 의존: dict.js (HANJA_DICT, WORD_DICT, JP_DICT, analyze*, detectLang)

// === IndexedDB (영상 파일 저장) ===
const DB_NAME = 'RepeatPlayer';
const DB_VERSION = 1;
let DB = null;

function openDB(){
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB_NAME, DB_VERSION);
    r.onupgradeneeded = e => {
      const db = e.target.result;
      if(!db.objectStoreNames.contains('videos')){
        const s = db.createObjectStore('videos', {keyPath:'id'});
        s.createIndex('ts', 'ts');
      }
      if(!db.objectStoreNames.contains('meta')){
        db.createObjectStore('meta', {keyPath:'id'});
      }
    };
    r.onsuccess = e => { DB = e.target.result; resolve(DB); };
    r.onerror = e => reject(e.target.error);
  });
}

function dbGet(store, key){
  return new Promise((res, rej) => {
    const t = DB.transaction(store, 'readonly');
    const r = t.objectStore(store).get(key);
    r.onsuccess = () => res(r.result);
    r.onerror = e => rej(e.target.error);
  });
}
function dbPut(store, value){
  return new Promise((res, rej) => {
    const t = DB.transaction(store, 'readwrite');
    const r = t.objectStore(store).put(value);
    r.onsuccess = () => res();
    r.onerror = e => rej(e.target.error);
  });
}
function dbDelete(store, key){
  return new Promise((res, rej) => {
    const t = DB.transaction(store, 'readwrite');
    const r = t.objectStore(store).delete(key);
    r.onsuccess = () => res();
    r.onerror = e => rej(e.target.error);
  });
}
function dbListMeta(){
  return new Promise((res, rej) => {
    const t = DB.transaction('meta', 'readonly');
    const r = t.objectStore('meta').getAll();
    r.onsuccess = () => res(r.result.sort((a,b) => (b.ts||0) - (a.ts||0)));
    r.onerror = e => rej(e.target.error);
  });
}

// === 상태 ===
let curId = null;        // 현재 영상 ID
let curMeta = null;      // {id, name, segments, ts}
let SEGMENTS = [];       // [{i, start, end}]
let cur = 0;             // 현재 세그먼트 인덱스
let count = 0;           // 반복 횟수
let autoNext = true;     // 자동 다음 세그먼트
let userSeeking = false; // 시킹 중 플래그

const v = document.getElementById('v');

// === 토스트 ===
let toastTimer;
function toast(msg, ms=2200){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

// === 초기화 ===
window.addEventListener('load', async () => {
  try {
    await openDB();
    renderRecent();
  } catch(e){
    toast('DB 초기화 실패: ' + e.message);
  }
  // OpenRouter 키 확인 → AI 버튼 표시
  if(localStorage.getItem('openrouter_key')){
    document.getElementById('ai-btn').style.display = '';
  }
});

// === 홈 화면 ===
async function renderRecent(){
  const list = document.getElementById('recent-list');
  const metas = await dbListMeta();
  if(!metas.length){
    list.innerHTML = '<div class="empty">아직 영상을 추가하지 않았습니다.</div>';
    return;
  }
  list.innerHTML = metas.map(m => {
    const prog = loadProgress(m.id);
    const total = m.segments.length;
    const at = prog.cur != null ? prog.cur + 1 : 0;
    const pct = (at && total) ? Math.round(at/total*100) : 0;
    const continueLabel = at > 1
      ? `<span class="continue-badge">이어서 ${at}/${total}</span>` : '';
    // 출처 URL 표시 (YouTube ID 있으면 youtu.be 링크, 외부 source 있으면 그대로)
    const src = m.source || (m.video_id ? `https://youtu.be/${m.video_id}` : '');
    const srcLink = src
      ? `<a class="src-link" href="${src.replace(/"/g,'&quot;')}" target="_blank" rel="noopener" onclick="event.stopPropagation()">출처</a>`
      : '';
    const sizeLabel = m.size ? `· ${(m.size/1048576).toFixed(0)}MB` : (m.video_id ? '· YouTube' : '');
    return `
    <div class="recent-card" onclick="loadVideo('${m.id}')">
      <div style="flex:1;min-width:0">
        <div class="name">${escapeHtml(m.name)} ${srcLink}</div>
        <div class="meta">${total}개 문장 ${sizeLabel} ${continueLabel}</div>
        ${pct ? `<div class="progress-mini"><div class="progress-mini-bar" style="width:${pct}%"></div></div>` : ''}
      </div>
      <button class="btn-del" onclick="event.stopPropagation();deleteVideo('${m.id}')">삭제</button>
    </div>`;
  }).join('');
}

function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function deleteVideo(id){
  if(!confirm('이 영상을 삭제할까요?')) return;
  await dbDelete('videos', id);
  await dbDelete('meta', id);
  // 분석 캐시 정리
  for(let i=0; i<localStorage.length; i++){
    const k = localStorage.key(i);
    if(k && k.startsWith(`analysis:${id}:`)){
      localStorage.removeItem(k); i--;
    }
  }
  renderRecent();
  toast('삭제됨');
}

// === 파일 선택 ===
function pickFile(){
  document.getElementById('file-input').click();
}

document.getElementById('file-input').addEventListener('change', async e => {
  const file = e.target.files[0];
  if(!file) return;
  e.target.value = '';
  await processNewVideo(file);
});

async function processNewVideo(file){
  const id = 'v_' + Date.now().toString(36);
  const overlay = document.getElementById('proc-overlay');
  const msg = document.getElementById('proc-msg');
  const detail = document.getElementById('proc-detail');
  document.getElementById('home').style.display = 'none';
  document.getElementById('player').classList.add('active');
  overlay.classList.add('show');
  msg.textContent = '영상 저장 중...';
  detail.textContent = file.name + ' · ' + (file.size/1048576).toFixed(0) + 'MB';

  // IndexedDB에 저장
  await dbPut('videos', {id, file, ts:Date.now()});

  msg.textContent = '무음 구간 감지 중...';
  detail.textContent = '~30초 소요 (영상 길이에 따라)';

  let segments;
  try {
    segments = await detectSegments(file);
  } catch(err){
    overlay.classList.remove('show');
    toast('세그먼트 감지 실패: ' + err.message);
    // 실패 시 30초 단위로 강제 분리
    const dur = await getDuration(file);
    segments = [];
    for(let s=0; s<dur; s+=30){
      segments.push({i:segments.length, start:s, end:Math.min(s+30, dur)});
    }
  }

  curMeta = {
    id, name:file.name.replace(/\.[^.]+$/, ''),
    segments, size:file.size, ts:Date.now(),
    duration: segments.length ? segments[segments.length-1].end : 0
  };
  await dbPut('meta', curMeta);
  curId = id;
  SEGMENTS = segments;
  overlay.classList.remove('show');
  loadVideoFile(file);
}

async function getDuration(file){
  return new Promise(res => {
    const tmp = document.createElement('video');
    tmp.preload = 'metadata';
    tmp.src = URL.createObjectURL(file);
    tmp.onloadedmetadata = () => { res(tmp.duration); URL.revokeObjectURL(tmp.src); };
  });
}

// === Web Audio 무음 감지 ===
async function detectSegments(file){
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const buf = await file.arrayBuffer();
  const audioBuffer = await audioCtx.decodeAudioData(buf.slice(0));
  const sampleRate = audioBuffer.sampleRate;
  const ch = audioBuffer.getChannelData(0);  // 모노 또는 좌채널만
  const total = audioBuffer.duration;

  // 윈도우 100ms씩 RMS 계산
  const winMs = 100;
  const winSize = Math.floor(sampleRate * winMs / 1000);
  const rms = [];
  for(let i=0; i<ch.length; i+=winSize){
    let sum = 0;
    const end = Math.min(i+winSize, ch.length);
    for(let j=i; j<end; j++) sum += ch[j]*ch[j];
    rms.push(Math.sqrt(sum / (end-i)));
  }
  // 동적 임계값: 90 percentile의 1/8
  const sorted = [...rms].sort((a,b)=>a-b);
  const p90 = sorted[Math.floor(sorted.length * 0.9)] || 0.01;
  const threshold = p90 / 8;

  // 무음 구간 표시
  const silent = rms.map(r => r < threshold);

  // 무음이 minSilenceMs 이상 지속되면 세그먼트 경계
  const minSilenceMs = 500;
  const minSilenceWins = minSilenceMs / winMs;
  const segments = [];
  let segStart = 0;
  let silentRun = 0;
  let silentStart = 0;
  for(let i=0; i<silent.length; i++){
    if(silent[i]){
      if(silentRun === 0) silentStart = i;
      silentRun++;
    } else {
      if(silentRun >= minSilenceWins){
        // 세그먼트 종료 = 무음 시작 시점
        const segEnd = silentStart * winMs / 1000;
        if(segEnd - segStart > 1.0){  // 1초 이상만
          segments.push({i:segments.length, start:segStart, end:segEnd});
        }
        segStart = (silentStart + silentRun) * winMs / 1000;
      }
      silentRun = 0;
    }
  }
  // 마지막 세그먼트
  if(total - segStart > 1.0){
    segments.push({i:segments.length, start:segStart, end:total});
  }

  audioCtx.close();
  // 너무 긴 세그먼트(40초+) 분할
  const result = [];
  for(const s of segments){
    if(s.end - s.start <= 40){ result.push(s); continue; }
    let p = s.start;
    while(p < s.end){
      const e = Math.min(p + 25, s.end);
      result.push({start:p, end:e});
      p = e;
    }
  }
  return result.map((s, i) => ({i, start:s.start, end:s.end}));
}

// === 영상 로드 ===
async function loadVideo(id){
  const meta = await dbGet('meta', id);
  const rec = await dbGet('videos', id);
  if(!meta || !rec){ toast('영상 데이터 없음'); return; }
  curMeta = meta;
  curId = id;
  SEGMENTS = meta.segments;
  document.getElementById('home').style.display = 'none';
  document.getElementById('player').classList.add('active');
  loadVideoFile(rec.file);
}

function loadVideoFile(file){
  if(v.src) URL.revokeObjectURL(v.src);
  v.src = URL.createObjectURL(file);
  v.load();
  // 저장된 학습 진도 복원 (이어서 하기)
  const saved = loadProgress(curId);
  cur = saved.cur || 0; count = 0;
  v.addEventListener('loadedmetadata', () => {
    const startTime = saved.time != null ? saved.time : (SEGMENTS[cur]?.start || 0);
    seekTo(startTime);
    updateBar();
    loadAnalysisForSegment(cur);
    if(saved.cur > 0) toast(`이어서: ${cur+1}번째 문장`, 1800);
  }, {once:true});
}

// === 학습 진도 자동 저장 / 복원 ===
function progressKey(id){ return id ? `progress:${id}` : null; }
function saveProgress(){
  const k = progressKey(curId); if(!k) return;
  try{
    localStorage.setItem(k, JSON.stringify({
      cur, time: getCurrentTime() | 0, ts: Date.now()
    }));
  } catch(e){}
}
function loadProgress(id){
  const k = progressKey(id); if(!k) return {};
  try { return JSON.parse(localStorage.getItem(k) || '{}'); } catch(e){ return {}; }
}
// 5초마다 진도 저장
setInterval(() => { if(curId) saveProgress(); }, 5000);
// 페이지 이탈 시도 즉시 저장
window.addEventListener('beforeunload', () => { if(curId) saveProgress(); });
window.addEventListener('pagehide', () => { if(curId) saveProgress(); });

function goHome(){
  saveProgress();  // 이탈 전 진도 저장
  if(v.src){ URL.revokeObjectURL(v.src); v.src = ''; }
  document.getElementById('player').classList.remove('active');
  document.getElementById('home').style.display = 'block';
  curId = null; curMeta = null; SEGMENTS = [];
  renderRecent();
}

// === 재생 ===
function play(){ v.play().catch(()=>{}); }
function pause(){ v.pause(); }
function togglePlay(){ if(v.paused) play(); else pause(); }
function seekTo(t){ try{ v.currentTime = t; }catch(e){} }
function getCurrentTime(){ return v.currentTime || 0; }

// === 세그먼트 ===
function jumpToCur(){
  if(cur < 0) cur = 0;
  if(cur >= SEGMENTS.length){ pause(); return; }
  seekTo(SEGMENTS[cur].start);
  play();
  updateBar();
  loadAnalysisForSegment(cur);
  saveProgress();
}
function prevSeg(){ cur = Math.max(0, cur-1); count = 0; jumpToCur(); }
function nextSeg(){ cur = Math.min(SEGMENTS.length-1, cur+1); count = 0; jumpToCur(); }

function onSegmentEnd(){
  if(cur >= SEGMENTS.length) return;
  count++;
  const repeats = parseInt(document.getElementById('repeat').value);
  if(count < repeats){
    seekTo(SEGMENTS[cur].start);
  } else {
    count = 0;
    if(autoNext){ pause(); cur++; jumpToCur(); }
    else pause();
  }
}

function toggleAuto(){
  autoNext = !autoNext;
  const btn = document.getElementById('auto');
  btn.textContent = autoNext ? '자동 ON' : '자동 OFF';
  btn.classList.toggle('active', autoNext);
}

// === 진행바 ===
function fmt(s){
  s = Math.max(0, s|0);
  const m = (s/60)|0, ss = s%60;
  return `${m}:${ss.toString().padStart(2,'0')}`;
}

function updateBar(){
  if(!v.duration) return;
  const pct = v.currentTime / v.duration * 100;
  document.getElementById('bar').style.width = pct + '%';
  document.getElementById('marker').style.left = pct + '%';
  document.getElementById('cursor').textContent = `${cur+1}/${SEGMENTS.length} ${fmt(v.currentTime)}`;
  if(SEGMENTS[cur]){
    const seg = SEGMENTS[cur];
    const segCur = document.getElementById('seg-cur');
    segCur.style.display = 'block';
    segCur.style.left = (seg.start / v.duration * 100) + '%';
    segCur.style.width = ((seg.end - seg.start) / v.duration * 100) + '%';
  }
}

function seekClick(e){
  // 클릭 한 번 (드래그 없을 때) — jumpToCur()로 세그먼트 점프
  if(barDragging) return;
  if(!getDur()) return;
  const t = barEventTime(e);
  snapToNearestSeg(t);
}

// === 진행바 드래그 ===
let barDragging = false;
function getDur(){ return isYtMode() ? (ytPlayer ? ytPlayer.getDuration() : 0) : (v.duration || 0); }
function barEventTime(e){
  const rect = document.getElementById('bar-wrap').getBoundingClientRect();
  if(!rect.width) return 0;
  const x = (e.touches?.[0]?.clientX ?? e.clientX) - rect.left;
  return Math.max(0, Math.min(1, x/rect.width)) * getDur();
}
function snapToNearestSeg(t){
  let nearest = 0, nearestDiff = Infinity;
  for(let i=0; i<SEGMENTS.length; i++){
    if(t >= SEGMENTS[i].start && t < SEGMENTS[i].end){ nearest = i; break; }
    const d = Math.min(Math.abs(t - SEGMENTS[i].start), Math.abs(t - SEGMENTS[i].end));
    if(d < nearestDiff){ nearestDiff = d; nearest = i; }
  }
  cur = nearest; count = 0; jumpToCur();
}
function bindBarDrag(){
  const wrap = document.getElementById('bar-wrap');
  if(!wrap) return;
  const start = e => {
    if(!getDur()) return;
    barDragging = true;
    seekTo(barEventTime(e));
    if(e.cancelable) e.preventDefault();
  };
  wrap.addEventListener('mousedown', start);
  wrap.addEventListener('touchstart', start, {passive:false});
}
document.addEventListener('mousemove', e => {
  if(!barDragging) return;
  seekTo(barEventTime(e));
});
document.addEventListener('touchmove', e => {
  if(!barDragging) return;
  seekTo(barEventTime(e));
}, {passive:true});
function endDrag(){
  if(!barDragging) return;
  barDragging = false;
  // 드래그 끝 → 가장 가까운 세그먼트로 스냅
  const t = isYtMode() ? ytPlayer.getCurrentTime() : v.currentTime;
  snapToNearestSeg(t);
}
document.addEventListener('mouseup', endDrag);
document.addEventListener('touchend', endDrag);
window.addEventListener('load', bindBarDrag);

// 비디오 이벤트
v.addEventListener('timeupdate', () => {
  updateBar();
  if(cur < SEGMENTS.length && v.currentTime >= SEGMENTS[cur].end - 0.05 && !userSeeking){
    onSegmentEnd();
  }
});
v.addEventListener('play', () => document.getElementById('play').textContent = '⏸');
v.addEventListener('pause', () => document.getElementById('play').textContent = '▶');
v.addEventListener('seeking', () => userSeeking = true);
v.addEventListener('seeked', () => { setTimeout(() => userSeeking = false, 200); });

// 해석 기능은 OCR 구현 후 추가 예정 (현재 비활성)
function loadAnalysisForSegment(idx){ /* no-op */ }

// === 모달 ===
function closeModal(){ document.getElementById('modal').classList.remove('show'); }

// === YouTube URL 입력 + 붙여넣기 ===
async function pasteUrl(){
  let txt = '';
  try {
    txt = await navigator.clipboard.readText();
  } catch(e){
    // 권한 거부 (iOS Safari 등) — 입력란 포커스만
    document.getElementById('url-input').focus();
    toast('주소창 길게 눌러서 직접 붙여넣기 (권한 없음)', 3500);
    return;
  }
  txt = (txt || '').trim();
  if(!txt){ toast('클립보드 비어있음'); return; }
  document.getElementById('url-input').value = txt;
}

function extractYouTubeId(url){
  if(!url) return null;
  // 모바일 공유 시 텍스트가 함께 붙어올 수 있음 → 첫 공백/줄바꿈 전까지만
  url = String(url).trim().split(/\s+/)[0];
  // 1) 11자리 ID 그대로 입력한 경우
  if(/^[A-Za-z0-9_-]{11}$/.test(url)) return url;
  // 2) 모든 YouTube URL 형식 (youtu.be, watch, embed, shorts, live, m., music, no-cookie)
  const m = url.match(/(?:youtu\.be\/|youtube(?:-nocookie)?\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|shorts\/|v\/|live\/|e\/)|music\.youtube\.com\/watch\?(?:[^#]*&)?v=)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : null;
}

async function loadYoutubeUrl(){
  const url = document.getElementById('url-input').value.trim();
  if(!url){ toast('YouTube 주소를 입력하세요'); return; }
  const vid = extractYouTubeId(url);
  if(!vid){
    const preview = url.length > 40 ? url.slice(0, 40) + '...' : url;
    toast('YouTube 주소를 못 읽었습니다: ' + preview + ' — 다시 확인하세요', 4500);
    return;
  }

  const id = 'yt_' + vid;
  // 이미 등록된 영상이면 그걸 로드
  const existing = await dbGet('meta', id);
  if(existing){
    curId = id; curMeta = existing; SEGMENTS = existing.segments;
    document.getElementById('home').style.display = 'none';
    document.getElementById('player').classList.add('active');
    initYouTubePlayer(vid);
    return;
  }

  // 신규: 30초 단위 분할 (YouTube 영상 길이는 IFrame API에서 가져옴)
  document.getElementById('home').style.display = 'none';
  document.getElementById('player').classList.add('active');
  document.getElementById('proc-overlay').classList.add('show');
  document.getElementById('proc-msg').textContent = 'YouTube 영상 로드 중...';
  document.getElementById('proc-detail').textContent = vid;

  // 임시 메타 (duration은 onReady에서 채움)
  curId = id;
  curMeta = {
    id, name: 'YouTube ' + vid, video_id: vid,
    source: url,  // 원래 URL 그대로 저장 (출처 링크용)
    segments: [], ts: Date.now(), size: 0, duration: 0, is_youtube: true
  };
  SEGMENTS = [];
  initYouTubePlayer(vid, true);
}

let ytPlayer = null;
let ytReady = false;
let ytPollTimer = null;

function ensureYouTubeAPI(){
  return new Promise(res => {
    if(window.YT && window.YT.Player){ res(); return; }
    const tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
    window.onYouTubeIframeAPIReady = () => res();
  });
}

async function initYouTubePlayer(vid, isNew=false){
  await ensureYouTubeAPI();
  document.getElementById('v').style.display = 'none';
  const ytDiv = document.getElementById('yt-iframe');
  ytDiv.style.display = 'block';
  ytDiv.innerHTML = '<div id="yt-player" style="width:100%;height:100%"></div>';

  if(ytPlayer){ try{ ytPlayer.destroy(); }catch(e){} }
  ytPlayer = new YT.Player('yt-player', {
    videoId: vid,
    playerVars: {playsinline:1, controls:0, modestbranding:1, rel:0, fs:0},
    events: {
      onReady: async () => {
        ytReady = true;
        const dur = ytPlayer.getDuration();
        if(isNew){
          // 30초 단위 자동 분할
          const segs = [];
          for(let s=0; s<dur; s+=30){
            segs.push({i:segs.length, start:s, end:Math.min(s+30, dur)});
          }
          SEGMENTS = segs;
          curMeta.segments = segs;
          curMeta.duration = dur;
          await dbPut('meta', curMeta);
        }
        document.getElementById('proc-overlay').classList.remove('show');
        cur = 0; count = 0;
        ytPlayer.seekTo(SEGMENTS[0]?.start || 0);
        ytPlayer.playVideo();
        startYtPolling();
        loadAnalysisForSegment(0);
      },
      onStateChange: (e) => {
        if(e.data === YT.PlayerState.PLAYING){
          document.getElementById('play').textContent = '⏸';
        } else if(e.data === YT.PlayerState.PAUSED || e.data === YT.PlayerState.ENDED){
          document.getElementById('play').textContent = '▶';
        }
      }
    }
  });
}

function startYtPolling(){
  if(ytPollTimer) clearInterval(ytPollTimer);
  ytPollTimer = setInterval(() => {
    if(!ytReady || !ytPlayer) return;
    const t = ytPlayer.getCurrentTime();
    const dur = ytPlayer.getDuration();
    if(!dur) return;
    // 진행바 업데이트
    const pct = t/dur*100;
    document.getElementById('bar').style.width = pct + '%';
    document.getElementById('marker').style.left = pct + '%';
    document.getElementById('cursor').textContent = `${cur+1}/${SEGMENTS.length} ${fmt(t)}`;
    if(SEGMENTS[cur]){
      const seg = SEGMENTS[cur];
      const segCur = document.getElementById('seg-cur');
      segCur.style.display = 'block';
      segCur.style.left = (seg.start/dur*100) + '%';
      segCur.style.width = ((seg.end-seg.start)/dur*100) + '%';
    }
    // 세그먼트 끝 처리
    if(cur < SEGMENTS.length && t >= SEGMENTS[cur].end - 0.05){
      onSegmentEnd();
    }
  }, 250);
}

// YouTube 모드용 재생/시킹 오버라이드
const _origPlay = play, _origPause = pause, _origSeekTo = seekTo, _origGetCurrentTime = getCurrentTime;
function isYtMode(){ return curMeta && curMeta.is_youtube; }
play = function(){ if(isYtMode() && ytPlayer) ytPlayer.playVideo(); else _origPlay(); };
pause = function(){ if(isYtMode() && ytPlayer) ytPlayer.pauseVideo(); else _origPause(); };
seekTo = function(t){ if(isYtMode() && ytPlayer) ytPlayer.seekTo(t, true); else _origSeekTo(t); };
getCurrentTime = function(){ if(isYtMode() && ytPlayer) return ytPlayer.getCurrentTime(); return _origGetCurrentTime(); };

// goHome 보강 (YT 정리)
const _origGoHome = goHome;
goHome = function(){
  if(ytPollTimer){ clearInterval(ytPollTimer); ytPollTimer = null; }
  if(ytPlayer){ try{ ytPlayer.destroy(); }catch(e){} ytPlayer = null; ytReady = false; }
  document.getElementById('yt-iframe').style.display = 'none';
  document.getElementById('yt-iframe').innerHTML = '';
  document.getElementById('v').style.display = '';
  _origGoHome();
};

// 디버그 노출
window.RP = {SEGMENTS, get cur(){return cur;}, get meta(){return curMeta;}, openDB, dbListMeta};
