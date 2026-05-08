"""
영상 → 반복 학습 플레이어 (다운로드/스트리밍 듀얼 모드).

사용법:
  python make_player.py <video_or_url> <project_id> [--mode both|download|stream]
                                                    [--noise -30] [--silence 0.4]

기본 mode=both: 두 모드 모두 지원하는 플레이어 생성
  - 다운로드 모드: 영상+음성 mp4 (오프라인 가능)
  - 스트리밍 모드: YouTube IFrame 임베드 (영상 안 받음)

수행 흐름:
  1. URL이면 video_id 추출
  2. 오디오만 다운로드 (~50MB, 무음 감지용)
  3. (mode에 download 포함되면) 영상+음성 통합 다운로드 (~250MB)
  4. 무음 구간 감지 → 문장 경계
  5. 단일 HTML 플레이어 생성 (모드 토글 가능)
"""
import sys, os, subprocess, json, re, shutil
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

if len(sys.argv) < 3:
    print(__doc__); sys.exit(1)

SRC = sys.argv[1]
PID = sys.argv[2]
MODE = 'both'
NOISE_DB = -30
SILENCE_DUR = 0.4
i = 3
while i < len(sys.argv):
    if sys.argv[i] == '--mode' and i+1 < len(sys.argv): MODE = sys.argv[i+1]; i += 2
    elif sys.argv[i] == '--noise' and i+1 < len(sys.argv): NOISE_DB = int(sys.argv[i+1]); i += 2
    elif sys.argv[i] == '--silence' and i+1 < len(sys.argv): SILENCE_DUR = float(sys.argv[i+1]); i += 2
    else: i += 1

if MODE not in ('both', 'download', 'stream'):
    print(f'Invalid mode: {MODE}'); sys.exit(1)

BASE = Path(__file__).parent
OUT_DIR = BASE / 'output'
OUT_DIR.mkdir(exist_ok=True)

def get_ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def log(m):
    import time
    print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)

def extract_youtube_id(url):
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else None

def yt_dlp_cmd(extra_args, out_template):
    env = os.environ.copy()
    # Node.js: PC(Windows)에서만 사용. Linux/서버에선 PATH에 있으면 자동 사용
    if sys.platform == 'win32':
        env['PATH'] = r'C:\Program Files\nodejs;' + env.get('PATH', '')
    cmd = [
        sys.executable, '-m', 'yt_dlp',
        '--no-playlist',
        '--ffmpeg-location', get_ffmpeg(),
        '-o', out_template,
    ] + extra_args + [SRC]
    # Node.js + PO Token 플러그인이 설치되어 있을 때만 추가 (PC 환경)
    import shutil
    if shutil.which('node'):
        cmd[2:2] = ['--js-runtimes', 'node', '--remote-components', 'ejs:github']
    cookies = BASE.parent / 'cookies.txt'
    if cookies.exists():
        cmd[3:3] = ['--cookies', str(cookies)]
    return cmd, env

def download_audio_only(pid):
    """Download just audio (m4a) for silence detection."""
    target = OUT_DIR / f'{pid}.m4a'
    if target.exists():
        log(f'[skip] audio (exists): {target.name}')
        return str(target)
    log(f'Downloading audio only...')
    cmd, env = yt_dlp_cmd(
        ['-f', 'bestaudio[ext=m4a]/140',
         '--extractor-args', 'youtube:player_client=default;youtube:formats=missing_pot'],
        str(target.with_suffix('.%(ext)s')))
    subprocess.run(cmd, env=env, check=True)
    return str(target)

def download_video(pid):
    """Download video+audio merged for offline mode."""
    target = OUT_DIR / f'{pid}.mp4'
    if target.exists():
        log(f'[skip] video (exists): {target.name}')
        return str(target)
    log(f'Downloading video (720p+audio)...')
    cmd, env = yt_dlp_cmd(
        ['-f', 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/18',
         '--extractor-args', 'youtube:player_client=default;youtube:formats=missing_pot',
         '--merge-output-format', 'mp4'],
        str(target.with_suffix('.%(ext)s')))
    subprocess.run(cmd, env=env, check=True)
    return str(target)

def detect_segments(audio_or_video, noise, silence):
    log(f'Detecting silence (noise={noise}dB, min={silence}s)...')
    out_json = Path(audio_or_video).with_suffix('.segments.json')
    cmd = [sys.executable, str(BASE / 'detect_segments.py'),
           audio_or_video, str(out_json), str(silence), str(noise)]
    subprocess.run(cmd, check=True)
    with open(out_json, encoding='utf-8') as f:
        return json.load(f)

def filter_segments(segments, min_dur=2.0, merge_gap=1.0, max_dur=40.0):
    """Filter and merge. Drop too-short and too-long (intro music/outro = mega-segments without breaks)."""
    merged = []
    for s in segments:
        if merged and s['start'] - merged[-1]['end'] < merge_gap:
            merged[-1]['end'] = s['end']
            merged[-1]['dur'] = round(merged[-1]['end'] - merged[-1]['start'], 2)
        else:
            merged.append(dict(s))
    out = [s for s in merged if min_dur <= s['dur'] <= max_dur]
    for i, s in enumerate(out):
        s['i'] = i
    return out

PLAYER_HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PID__ - 반복 학습</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#1f2937;color:#f3f4f6;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif}
.app{display:flex;flex-direction:column;height:100vh;height:100dvh}
header{padding:8px 12px;background:#111827;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;flex-wrap:wrap;gap:8px}
header h1{font-size:14px;font-weight:bold}
header .stats{font-size:13px;color:#9ca3af}
.mode-toggle{display:flex;gap:4px}
.mode-toggle button{padding:6px 12px;background:#374151;color:#f3f4f6;border:none;border-radius:6px;font-size:12px;cursor:pointer}
.mode-toggle button.active{background:#1e3a8a;color:#fff;font-weight:bold}
.video-wrap{flex:1;display:flex;justify-content:center;align-items:center;background:#000;overflow:hidden;position:relative}
video,#yt-iframe{max-width:100%;max-height:100%;width:100%;height:100%}
#yt-iframe{aspect-ratio:16/9}
.controls{background:#111827;padding:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;flex-shrink:0}
.controls button,.controls select{padding:10px 14px;background:#374151;color:#f3f4f6;border:none;border-radius:6px;font-size:14px;cursor:pointer}
.controls button:hover{background:#4b5563}
.controls button.active{background:#1e3a8a}
.list{background:#1f2937;max-height:200px;overflow-y:auto;border-top:1px solid #374151}
.list-row{padding:8px 14px;border-bottom:1px solid #374151;cursor:pointer;display:flex;gap:10px}
.list-row:hover{background:#374151}
.list-row.cur{background:#1e3a8a;color:#fff}
.list-row .ix{color:#9ca3af;width:50px;text-align:right;flex-shrink:0}
.list-row .tm{color:#9ca3af;font-size:12px;flex-shrink:0;width:80px}
.list-row.cur .ix,.list-row.cur .tm{color:#bfdbfe}
.bar{height:6px;background:#374151;flex:1;border-radius:3px;overflow:hidden;min-width:100px}
.bar > div{height:100%;background:#22c55e;transition:width .2s}
.hidden{display:none !important}
@media (max-width:600px){
  .controls button,.controls select{padding:9px 10px;font-size:13px}
  .controls{padding:8px 6px;gap:5px}
}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>📺 __PID__</h1>
    <div class="mode-toggle" id="mode-toggle"></div>
    <div class="stats" id="stats">-</div>
  </header>
  <div class="video-wrap">
    <video id="v" src="__VIDEO_FILE__" controls preload="metadata" __VIDEO_HIDDEN__></video>
    <div id="yt-iframe" __IFRAME_HIDDEN__></div>
  </div>
  <div class="controls">
    <button id="prev">⏮ 이전</button>
    <button id="play">▶ 재생</button>
    <button id="next">다음 ⏭</button>
    <span style="color:#9ca3af;font-size:12px">반복</span>
    <select id="repeat">
      <option value="1">1x</option>
      <option value="2" selected>2x</option>
      <option value="3">3x</option>
      <option value="4">4x</option>
      <option value="5">5x</option>
      <option value="99">∞</option>
    </select>
    <button id="auto" class="active">자동 진행 ON</button>
    <span style="color:#9ca3af;font-size:12px">간격</span>
    <select id="gap">
      <option value="0" selected>0초</option>
      <option value="0.5">0.5초</option>
      <option value="1">1초</option>
      <option value="2">2초</option>
    </select>
    <div class="bar"><div id="bar" style="width:0%"></div></div>
  </div>
  <div class="list" id="list"></div>
</div>

<script src="https://www.youtube.com/iframe_api"></script>
<script>
const SEGMENTS = __SEGMENTS__;
const TOTAL = SEGMENTS.length;
const HAS_DOWNLOAD = __HAS_DOWNLOAD__;
const HAS_STREAM = __HAS_STREAM__;
const VIDEO_ID = '__VIDEO_ID__';

let mode = HAS_DOWNLOAD ? 'download' : 'stream';
let cur = 0, count = 0, autoNext = true;
let ytPlayer = null;
let ytTimer = null;

const v = document.getElementById('v');
const ytDiv = document.getElementById('yt-iframe');

function fmt(t){
  const m = Math.floor(t/60), s = Math.floor(t%60);
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function statsText(){ return `${cur+1} / ${TOTAL}  (${count+1}회째)`; }
function updateStats(){
  document.getElementById('stats').textContent = statsText();
  document.getElementById('bar').style.width = ((cur+1)/TOTAL*100)+'%';
}
function renderList(){
  const list = document.getElementById('list');
  list.innerHTML = SEGMENTS.map((s,i)=>
    `<div class="list-row${i===cur?' cur':''}" data-i="${i}">
      <span class="ix">#${i+1}</span>
      <span class="tm">${fmt(s.start)}-${fmt(s.end)}</span>
      <span>(${s.dur.toFixed(1)}s)</span>
    </div>`
  ).join('');
  list.querySelectorAll('.list-row').forEach(r=>{
    r.onclick = ()=>{ cur = parseInt(r.dataset.i); count = 0; jumpToCur(); };
  });
  const curRow = list.querySelector('.list-row.cur');
  if(curRow) curRow.scrollIntoView({block:'center', behavior:'smooth'});
}

// === Mode handling ===
function setMode(newMode){
  mode = newMode;
  document.querySelectorAll('#mode-toggle button').forEach(b=>{
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  if(mode === 'download'){
    v.classList.remove('hidden');
    ytDiv.classList.add('hidden');
    if(ytPlayer && ytPlayer.pauseVideo) ytPlayer.pauseVideo();
  } else {
    v.classList.add('hidden');
    ytDiv.classList.remove('hidden');
    v.pause();
    if(!ytPlayer) initYouTube();
  }
}
function buildModeToggle(){
  const div = document.getElementById('mode-toggle');
  let html = '';
  if(HAS_DOWNLOAD) html += `<button data-mode="download">📁 다운로드</button>`;
  if(HAS_STREAM) html += `<button data-mode="stream">🌐 스트리밍</button>`;
  div.innerHTML = html;
  div.querySelectorAll('button').forEach(b=>{
    b.onclick = ()=> setMode(b.dataset.mode);
  });
  setMode(mode);
}

// === YouTube IFrame ===
function onYouTubeIframeAPIReady(){ /* ready, init on demand */ }
function initYouTube(){
  if(!HAS_STREAM || ytPlayer) return;
  ytPlayer = new YT.Player('yt-iframe', {
    videoId: VIDEO_ID,
    playerVars: {playsinline:1, modestbranding:1, controls:1, fs:1, rel:0},
    events: {
      'onReady': () => {
        ytPlayer.seekTo(SEGMENTS[cur].start, true);
        startYtMonitor();
      }
    }
  });
}
function startYtMonitor(){
  if(ytTimer) clearInterval(ytTimer);
  ytTimer = setInterval(() => {
    if(!ytPlayer || !ytPlayer.getCurrentTime) return;
    if(mode !== 'stream') return;
    if(cur >= TOTAL) return;
    const t = ytPlayer.getCurrentTime();
    const seg = SEGMENTS[cur];
    if(t >= seg.end - 0.05){ onSegmentEnd(); }
  }, 200);
}

// === Unified controls (works for both download and stream) ===
function getCurrentTime(){
  return mode === 'download' ? v.currentTime : (ytPlayer && ytPlayer.getCurrentTime ? ytPlayer.getCurrentTime() : 0);
}
function seekTo(t){
  if(mode === 'download'){ v.currentTime = t; }
  else if(ytPlayer && ytPlayer.seekTo){ ytPlayer.seekTo(t, true); }
}
function play(){
  if(mode === 'download'){ v.play(); }
  else if(ytPlayer && ytPlayer.playVideo){ ytPlayer.playVideo(); }
}
function pause(){
  if(mode === 'download'){ v.pause(); }
  else if(ytPlayer && ytPlayer.pauseVideo){ ytPlayer.pauseVideo(); }
}

function jumpToCur(){
  if(cur < 0) cur = 0;
  if(cur >= TOTAL){ pause(); return; }
  seekTo(SEGMENTS[cur].start);
  play();
  updateStats(); renderList();
}

function onSegmentEnd(){
  if(cur >= TOTAL) return;
  count++;
  const repeats = parseInt(document.getElementById('repeat').value);
  if(count < repeats){
    seekTo(SEGMENTS[cur].start);
  } else {
    count = 0;
    const gap = parseFloat(document.getElementById('gap').value);
    if(autoNext){
      pause();
      if(gap > 0){
        setTimeout(()=>{ cur++; jumpToCur(); }, gap*1000);
      } else {
        cur++; jumpToCur();
      }
    } else {
      pause();
    }
  }
  updateStats();
}

// Download mode timeupdate handler
v.addEventListener('timeupdate', ()=>{
  if(mode !== 'download' || cur >= TOTAL) return;
  if(v.currentTime >= SEGMENTS[cur].end - 0.05) onSegmentEnd();
});

// Buttons
document.getElementById('prev').onclick = ()=>{ cur=Math.max(0,cur-1); count=0; jumpToCur(); };
document.getElementById('next').onclick = ()=>{ cur=Math.min(TOTAL-1,cur+1); count=0; jumpToCur(); };
document.getElementById('play').onclick = ()=>{
  // toggle
  if(mode === 'download'){
    v.paused ? play() : pause();
  } else {
    const state = ytPlayer && ytPlayer.getPlayerState ? ytPlayer.getPlayerState() : -1;
    if(state === 1) pause(); else play();
  }
};
document.getElementById('auto').onclick = (e)=>{
  autoNext = !autoNext;
  e.target.classList.toggle('active', autoNext);
  e.target.textContent = autoNext ? '자동 진행 ON' : '자동 진행 OFF';
};
document.addEventListener('keydown', e=>{
  if(e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if(e.key === 'ArrowLeft'){ document.getElementById('prev').click(); e.preventDefault(); }
  else if(e.key === 'ArrowRight'){ document.getElementById('next').click(); e.preventDefault(); }
  else if(e.key === ' '){ document.getElementById('play').click(); e.preventDefault(); }
});

// init
buildModeToggle();
v.addEventListener('loadedmetadata', ()=>{
  if(mode === 'download'){ v.currentTime = SEGMENTS[0].start; }
});
updateStats(); renderList();
</script>
</body>
</html>
'''

def main():
    global MODE
    log(f'=== Project: {PID} (mode={MODE}) ===')
    video_id = None
    if SRC.startswith('http'):
        video_id = extract_youtube_id(SRC)
        if not video_id and MODE in ('stream', 'both'):
            log('Cannot extract YouTube ID from URL; falling back to download-only')
            MODE = 'download'

    has_download = MODE in ('download', 'both')
    has_stream = MODE in ('stream', 'both') and video_id is not None

    # 1. Audio for silence detection (always needed unless we have local file)
    if SRC.startswith('http'):
        audio = download_audio_only(PID)
        analyze_target = audio
    else:
        analyze_target = SRC

    # 2. Detect segments
    seg_data = detect_segments(analyze_target, NOISE_DB, SILENCE_DUR)
    raw_segments = seg_data['segments']
    log(f'  raw segments: {len(raw_segments)}')
    segments = filter_segments(raw_segments, min_dur=2.0, merge_gap=1.0)
    log(f'  filtered: {len(segments)} (min 2.0s, merge gaps <1.0s)')

    if not segments:
        log('No segments after filtering!'); sys.exit(1)

    # 3. Download full video if needed
    video_file_name = ''
    if has_download:
        if SRC.startswith('http'):
            video = download_video(PID)
            video_file_name = Path(video).name
        else:
            # local file - copy/symlink to output
            tgt = OUT_DIR / Path(SRC).name
            if not tgt.exists(): shutil.copy(SRC, tgt)
            video_file_name = tgt.name

    # 4. Save metadata for unified player
    meta = {
        'id': PID,
        'video_id': video_id or '',
        'source_url': SRC if SRC.startswith('http') else '',
        'video_file': video_file_name,
        'has_download': has_download,
        'has_stream': has_stream,
        'segments': segments,
    }
    meta_path = OUT_DIR / f'{PID}.meta.json'
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    # 5. Also build per-project legacy HTML (kept for backward compatibility)
    player_html = OUT_DIR / f'{PID}.html'
    html = (PLAYER_HTML
            .replace('__PID__', PID)
            .replace('__VIDEO_FILE__', video_file_name)
            .replace('__VIDEO_ID__', video_id or '')
            .replace('__SEGMENTS__', json.dumps(segments, ensure_ascii=False))
            .replace('__HAS_DOWNLOAD__', 'true' if has_download else 'false')
            .replace('__HAS_STREAM__', 'true' if has_stream else 'false')
            .replace('__VIDEO_HIDDEN__', '' if has_download else 'class="hidden"')
            .replace('__IFRAME_HIDDEN__', 'class="hidden"' if has_download else ''))
    player_html.write_text(html, encoding='utf-8')

    log(f'Meta: {meta_path}')
    log(f'Player: {player_html}')
    log(f'Segments: {len(segments)}')
    log(f'Modes: download={has_download}, stream={has_stream}')
    log('=== DONE ===')

if __name__ == '__main__':
    main()
