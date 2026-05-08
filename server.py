"""
모바일/PC 통합 반복 학습 서버.

페이지:
  /             → 통합 페이지 (URL 입력 + 영상 선택 + 플레이어 통합)
  /<id>.html    → 개별 플레이어 (레거시)

API:
  POST /api/process       → 새 영상 처리
  GET  /api/status?pid=X  → 처리 상태
  GET  /api/list          → 모든 영상 메타 (id, segments, video_id, has_video 등)
  GET  /api/meta?pid=X    → 특정 영상 상세

사용:
  python server.py [port=5757]
  → http://localhost:5757/  또는  http://192.168.0.7:5757/
"""
import sys, os, json, subprocess, time, threading, re
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.stdout.reconfigure(encoding='utf-8')

BASE = Path(__file__).parent
OUT_DIR = Path(os.environ.get('RP_DATA_DIR', BASE / 'output'))
OUT_DIR.mkdir(exist_ok=True, parents=True)
MAKE = BASE / 'make_player.py'
# 포트: 1) CLI 인자  2) 환경변수 PORT (Render/Heroku 표준)  3) 기본 5757
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', 5757))

# 가족 공유용 BasicAuth — 환경변수 RP_USER, RP_PASS 설정 시 요구
RP_USER = os.environ.get('RP_USER', '')
RP_PASS = os.environ.get('RP_PASS', '')

JOBS = {}
JOB_LOCK = threading.Lock()

OPENROUTER_KEY_FILE = BASE / 'openrouter.key'

def get_or_key():
    if not OPENROUTER_KEY_FILE.exists(): return ''
    for line in OPENROUTER_KEY_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and line.startswith('sk-'):
            return line
    return ''

def set_or_key(key):
    OPENROUTER_KEY_FILE.write_text(
        '# OpenRouter API key. 발급: https://openrouter.ai/keys\n' + key + '\n',
        encoding='utf-8'
    )

# 무료/저렴 모델 폴백 순서 (앞에서부터 시도, 404/429 시 다음)
FREE_TEXT_MODELS = [
    'google/gemini-2.5-flash-lite',
    'google/gemini-2.0-flash-lite-001',
    'meta-llama/llama-3.3-70b-instruct:free',
    'deepseek/deepseek-chat-v3-0324:free',
    'google/gemma-3-27b-it:free',
]
FREE_VISION_MODELS = [
    'google/gemini-2.5-flash-lite',
    'google/gemini-2.0-flash-lite-001',
    'google/gemma-3-27b-it:free',
    'qwen/qwen2.5-vl-72b-instruct:free',
]

def _openrouter_post(body, key):
    import urllib.request
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'http://localhost:5757',
            'X-Title': 'Repeat Player',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

def call_openrouter_text(prompt, model=None):
    key = get_or_key()
    if not key:
        return {'error': 'OpenRouter API 키가 설정되지 않음'}
    models = [model] if model else FREE_TEXT_MODELS
    last_err = ''
    import urllib.error
    for m in models:
        body = {'model': m, 'messages': [{'role': 'user', 'content': prompt}]}
        try:
            data = _openrouter_post(body, key)
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            return {'ok': True, 'text': content, 'model': m}
        except urllib.error.HTTPError as e:
            b = e.read().decode('utf-8', errors='replace')
            last_err = f'{m}: HTTP {e.code} {b[:200]}'
            if e.code in (404, 400):  # try next model
                continue
            return {'error': last_err}
        except Exception as e:
            last_err = f'{m}: {e}'
            continue
    return {'error': '모든 모델 실패: ' + last_err}

def call_openrouter_vision(img_bytes, prompt, model=None):
    key = get_or_key()
    if not key:
        return {'error': 'OpenRouter API 키가 설정되지 않음'}
    import base64, urllib.error
    img_b64 = base64.b64encode(img_bytes).decode('ascii')
    msg = [{'role': 'user', 'content': [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}}
    ]}]
    models = [model] if model else FREE_VISION_MODELS
    last_err = ''
    for m in models:
        body = {'model': m, 'messages': msg}
        try:
            data = _openrouter_post(body, key)
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            return {'ok': True, 'text': content, 'model': m}
        except urllib.error.HTTPError as e:
            b = e.read().decode('utf-8', errors='replace')
            last_err = f'{m}: HTTP {e.code} {b[:200]}'
            if e.code in (404, 400):
                continue
            return {'error': last_err}
        except Exception as e:
            last_err = f'{m}: {e}'
            continue
    return {'error': '모든 비전 모델 실패: ' + last_err}

# Cached OCR engine (lazy init, optional dependency)
_RAPID = None
_RAPID_FAILED = False
def get_rapid():
    global _RAPID, _RAPID_FAILED
    if _RAPID_FAILED:
        raise RuntimeError('RapidOCR 미설치. pip install rapidocr-onnxruntime')
    if _RAPID is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID = RapidOCR()
        except ImportError as e:
            _RAPID_FAILED = True
            raise RuntimeError(f'RapidOCR 미설치: {e}. AI 폴백 사용됩니다.')
    return _RAPID

def ocr_image_bytes(img_bytes):
    """Run OCR on raw image bytes. Returns concatenated CJK text."""
    import cv2, numpy as np
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None: return ''
    # Force 3-channel BGR (RapidOCR ONNX models expect this)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    # Downscale very large images (avoids ONNX memory failures)
    H, W = img.shape[:2]
    MAX_DIM = 1920
    if max(H, W) > MAX_DIM:
        scale = MAX_DIM / max(H, W)
        img = cv2.resize(img, (int(W*scale), int(H*scale)), interpolation=cv2.INTER_AREA)
    rapid = get_rapid()
    try:
        res, _ = rapid(img)
    except Exception as e:
        # ONNX inference can fail on odd shapes; retry on a smaller copy
        try:
            small = cv2.resize(img, (img.shape[1]//2, img.shape[0]//2), interpolation=cv2.INTER_AREA)
            res, _ = rapid(small)
        except Exception:
            raise RuntimeError(f'OCR inference failed: {e}')
    if not res: return ''
    # Pick line with most CJK chars (filter pinyin/Korean noise)
    best = ''
    best_count = 0
    for item in res:
        text = item[1]
        cjk = sum(1 for c in text if '一' <= c <= '鿿' or 'ぁ' <= c <= 'ヶ')
        if cjk > best_count:
            best_count = cjk
            best = text
    return best.strip()

def list_projects():
    projs = []
    for meta_file in OUT_DIR.glob('*.meta.json'):
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
            pid = meta['id']
            mp4 = OUT_DIR / f'{pid}.mp4'
            size_mb = mp4.stat().st_size / 1048576 if mp4.exists() else 0
            projs.append({
                'id': pid,
                'name': meta.get('name', pid),
                'video_id': meta.get('video_id', ''),
                'has_download': meta.get('has_download', False),
                'has_stream': meta.get('has_stream', False),
                'segments_count': len(meta.get('segments', [])),
                'size_mb': round(size_mb, 1),
                'has_video': mp4.exists(),
            })
        except: pass
    # Fallback: also list legacy HTMLs without meta
    for html in OUT_DIR.glob('*.html'):
        if html.name == 'index.html': continue
        pid = html.stem
        if any(p['id'] == pid for p in projs): continue
        seg_file = OUT_DIR / f'{pid}.segments.json'
        n_seg = 0
        try:
            data = json.loads(seg_file.read_text(encoding='utf-8'))
            n_seg = data.get('count', len(data.get('segments', [])))
        except: pass
        mp4 = OUT_DIR / f'{pid}.mp4'
        projs.append({
            'id': pid, 'video_id': '', 'has_download': mp4.exists(),
            'has_stream': False, 'segments_count': n_seg,
            'size_mb': round((mp4.stat().st_size/1048576) if mp4.exists() else 0, 1),
            'has_video': mp4.exists(), 'legacy': True,
        })
    projs.sort(key=lambda p: p['id'])
    return projs

def get_meta(pid):
    meta_file = OUT_DIR / f'{pid}.meta.json'
    if meta_file.exists():
        return json.loads(meta_file.read_text(encoding='utf-8'))
    # Legacy fallback
    seg_file = OUT_DIR / f'{pid}.segments.json'
    if seg_file.exists():
        data = json.loads(seg_file.read_text(encoding='utf-8'))
        mp4 = OUT_DIR / f'{pid}.mp4'
        return {
            'id': pid, 'video_id': '', 'video_file': f'{pid}.mp4' if mp4.exists() else '',
            'has_download': mp4.exists(), 'has_stream': False,
            'segments': data.get('segments', []),
        }
    return None

def parse_multipart(body, boundary):
    """Minimal multipart/form-data parser. Returns {field_name: {filename, data}|{value}}."""
    result = {}
    delim = b'--' + boundary.encode()
    parts = body.split(delim)
    for part in parts[1:-1]:
        part = part.strip(b'\r\n')
        if b'\r\n\r\n' not in part: continue
        headers_raw, data = part.split(b'\r\n\r\n', 1)
        # remove trailing CRLF before next boundary
        if data.endswith(b'\r\n'): data = data[:-2]
        cd = ''
        for line in headers_raw.split(b'\r\n'):
            line = line.decode('utf-8', 'replace')
            if line.lower().startswith('content-disposition:'):
                cd = line[len('content-disposition:'):].strip()
        m = re.search(r'name="([^"]*)"', cd)
        name = m.group(1) if m else ''
        m = re.search(r'filename="([^"]*)"', cd)
        filename = m.group(1) if m else None
        if filename is not None:
            result[name] = {'filename': filename, 'data': data}
        else:
            result[name] = {'value': data.decode('utf-8', 'replace')}
    return result

def process_video_async(pid, url, mode='both', noise=-30, silence=0.4):
    with JOB_LOCK:
        JOBS[pid] = {'status': 'running', 'log': [], 'started': time.time()}
    cmd = [sys.executable, str(MAKE), url, pid, '--mode', mode,
           '--noise', str(noise), '--silence', str(silence)]
    try:
        proc = subprocess.Popen(cmd, cwd=str(BASE),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=1, text=True, encoding='utf-8', errors='replace')
        for line in proc.stdout:
            with JOB_LOCK:
                JOBS[pid]['log'].append(line.rstrip())
                if len(JOBS[pid]['log']) > 200:
                    JOBS[pid]['log'] = JOBS[pid]['log'][-200:]
        proc.wait()
        with JOB_LOCK:
            JOBS[pid]['status'] = 'done' if proc.returncode == 0 else 'failed'
            JOBS[pid]['exit_code'] = proc.returncode
            JOBS[pid]['finished'] = time.time()
    except Exception as e:
        with JOB_LOCK:
            JOBS[pid]['status'] = 'failed'
            JOBS[pid]['log'].append(f'Exception: {e}')

INDEX_HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#1e3a8a">
<title>반복 학습 플레이어</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:#1f2937;color:#f3f4f6;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif}
.app{display:flex;flex-direction:column;height:100vh;height:100dvh}
header{padding:6px 8px;background:#111827;display:flex;gap:5px;align-items:center;flex-shrink:0;border-bottom:1px solid #374151}
header h1{font-size:18px;font-weight:bold;flex:0 0 auto}
.video-select{flex:2 1 0;min-width:0;padding:9px 8px;background:#374151;color:#fff;border:none;border-radius:6px;font-size:14px}
.btn-add{flex:0 0 auto;padding:9px 12px;background:#22c55e;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;font-weight:bold;white-space:nowrap}
.btn-icon{flex:1 1 0;min-width:32px;padding:9px 4px;background:#374151;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer;text-align:center;height:38px}
.btn-icon:hover{background:#4b5563}
.stats{display:none}
.settings-wrap{position:relative;flex:1 1 0;min-width:32px}
.settings-wrap > .btn-icon{width:100%}
.settings-menu{position:absolute;top:42px;right:0;background:#1f2937;border:1px solid #374151;border-radius:8px;
  min-width:180px;z-index:100;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.5);padding:4px}
.settings-menu.show{display:block}
.settings-menu button{display:block;width:100%;padding:10px 14px;background:transparent;color:#f3f4f6;
  border:none;border-radius:6px;font-size:14px;cursor:pointer;text-align:left}
.settings-menu button:hover{background:#374151}
.settings-menu .menu-section{padding:6px 14px 4px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px}

.body{flex:1;display:flex;overflow:hidden;min-height:0}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* 동영상이 최대한 크게, 해석은 결과 있을 때만 확장 */
.main #video-wrap{order:0;flex:1 1 auto}
.main #explain{order:1;flex:0 0 auto;min-height:0;max-height:26vh;padding:6px 12px}
.main #explain.has-result{flex:0 0 auto;max-height:30vh}
.main #progress-row{order:2}
.main #controls{order:3}
.sidebar{width:280px;background:#111827;border-left:1px solid #374151;display:flex;flex-direction:column;transition:width .2s}
.sidebar.collapsed{width:0;border-left:none;overflow:hidden}
.sidebar-hdr{padding:8px 12px;background:#1e293b;border-bottom:1px solid #374151;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.sidebar-hdr h3{font-size:13px;font-weight:bold;color:#bfdbfe}
.sidebar-toggle{position:absolute;right:0;top:50%;transform:translate(50%,-50%);background:#1e3a8a;color:#fff;border:none;padding:14px 4px;border-radius:6px 0 0 6px;cursor:pointer;font-size:14px;z-index:5;writing-mode:vertical-lr}
.sidebar.collapsed + .sidebar-toggle{transform:translate(0,-50%);right:0;border-radius:6px 0 0 6px}

.home-screen{flex:1;overflow-y:auto;padding:18px}
.home-screen h2{font-size:16px;font-weight:bold;margin-bottom:12px}
.hero{text-align:center;padding:30px 14px 20px;margin-bottom:20px;
  background:linear-gradient(135deg,#1e3a8a 0%,#7c3aed 100%);border-radius:14px;
  box-shadow:0 4px 20px rgba(30,58,138,0.4)}
.hero-logo{font-size:64px;animation:spin 4s linear infinite;display:inline-block}
@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
.hero-title{font-size:28px;font-weight:bold;color:#fff;margin:8px 0 6px;letter-spacing:-0.5px}
.hero-sub{font-size:14px;color:#dbeafe;line-height:1.5}
.section-title{font-size:14px;color:#9ca3af;text-transform:uppercase;letter-spacing:1px;margin-top:18px}
@media (max-width:600px){
  .hero{padding:20px 12px 14px}
  .hero-logo{font-size:48px}
  .hero-title{font-size:22px}
  .hero-sub{font-size:13px}
}
.proj-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.proj-card{background:#374151;padding:14px;border-radius:10px;cursor:pointer;border:2px solid transparent;transition:all .15s;position:relative}
.proj-card:hover{border-color:#1e3a8a;background:#4b5563}
.proj-card .name{font-size:15px;font-weight:bold;color:#fff;margin-bottom:6px}
.proj-card .id{font-size:11px;color:#9ca3af;margin-bottom:8px}
.proj-card .meta{font-size:12px;color:#bfdbfe}
.proj-card .actions{position:absolute;top:8px;right:8px;display:flex;gap:4px;opacity:0;transition:opacity .15s}
.proj-card:hover .actions{opacity:1}
.proj-card .actions button{padding:4px 8px;background:#1f2937;color:#f3f4f6;border:none;border-radius:4px;font-size:13px;cursor:pointer}
.proj-card .actions button:hover{background:#0f172a}
.proj-grid .empty{grid-column:1/-1;padding:40px;text-align:center;color:#6b7280}

.drop-zone{border:2px dashed #4b5563;padding:24px;text-align:center;border-radius:10px;
  margin-bottom:14px;color:#9ca3af;background:#111827;cursor:pointer;transition:all .15s}
.drop-zone.drag-over{border-color:#22c55e;background:#1e3a8a;color:#fff}
.drop-zone .big{font-size:32px;margin-bottom:6px}
.drop-zone .hint{font-size:12px;color:#6b7280;margin-top:6px}
.drop-overlay{position:fixed;inset:0;background:rgba(34,197,94,0.85);display:none;justify-content:center;
  align-items:center;font-size:32px;color:#fff;z-index:200;font-weight:bold;text-align:center;padding:30px}
.drop-overlay.show{display:flex}

.video-wrap{flex:1;display:flex;justify-content:center;align-items:center;background:#000;overflow:hidden;position:relative;min-height:200px}
video,#yt-iframe{max-width:100%;max-height:100%;width:auto;height:100%}
/* 일시정지 시 자막을 가리는 가운데 ▶ 오버레이 숨김 (Chrome/Edge/Android/iOS) */
video::-webkit-media-controls-overlay-play-button{display:none !important}
video::-webkit-media-controls-start-playback-button{display:none !important}
video::-internal-media-controls-overlay-cast-button{display:none !important}
@media (orientation:portrait){
  video,#yt-iframe{width:100%;height:auto}
}
.empty-video{color:#6b7280;text-align:center;padding:30px;font-size:14px}

.controls{background:#111827;padding:8px;display:flex;flex-direction:column;gap:8px;align-items:stretch;flex-shrink:0;border-top:1px solid #374151}
.ctrl-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.ctrl-row button{flex:0 0 auto;min-width:90px;padding:14px 18px;background:#374151;color:#f3f4f6;border:none;border-radius:8px;font-size:18px;font-weight:bold;cursor:pointer;min-height:54px}
.ctrl-row #prev,.ctrl-row #next{font-size:18px;min-width:120px;padding:14px 26px;letter-spacing:2px}
.ctrl-row #prev,.ctrl-row #next{font-feature-settings:'tnum';display:inline-flex;align-items:center;justify-content:center;gap:4px}
.ctrl-row .play-big{flex:0 0 200px;background:#1e3a8a;font-size:18px}
.ctrl-row .ctrl-capture{background:#3b82f6;font-size:15px;min-width:auto;min-height:38px;padding:8px 14px}
.ctrl-row .lbl{font-size:12px;color:#9ca3af}
.ctrl-row select,.ctrl-row #auto{padding:8px 12px;background:#374151;color:#f3f4f6;border:none;border-radius:6px;font-size:13px;cursor:pointer;min-height:38px;font-weight:normal;min-width:auto}
.ctrl-row #auto.active{background:#1e3a8a}
.ctrl-spacer{flex:1 1 auto}
@media(max-width:760px){
  /* 컨트롤바: 뷰포트 맨 아래 고정 (본문 길어도 안 따라감) */
  body{padding-bottom:env(safe-area-inset-bottom)}
  .main{padding-bottom:104px}
  .controls{position:fixed;bottom:0;left:0;right:0;z-index:50;
            background:#0b1220;box-shadow:0 -2px 8px rgba(0,0,0,.5);
            padding:6px 6px calc(8px + env(safe-area-inset-bottom))}
  .ctrl-row{flex-wrap:wrap;gap:5px;justify-content:center;overflow-x:visible}
  .ctrl-row .ctrl-spacer:first-child{display:none}
  .ctrl-row .ctrl-spacer{flex:0 0 100%;width:100%;height:0;margin:0;min-width:0}
  /* 6개 모두 3등분 (1줄/2줄 동일하게 폰 가로 꽉 채움) */
  .ctrl-row #prev,.ctrl-row #next,.ctrl-row .play-big,
  .ctrl-row #capture-btn,.ctrl-row #repeat,.ctrl-row #auto{
    flex:1 1 0;min-width:0;width:0;min-height:42px;padding:8px 4px;
    font-size:13px;font-weight:600;text-align:center;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
  .ctrl-row .play-big{font-size:15px;font-weight:700}
  .ctrl-row #prev,.ctrl-row #next{font-size:14px;letter-spacing:0;gap:2px}
  .ctrl-row #repeat{text-align-last:center;-webkit-appearance:none;appearance:none;padding-right:14px}
}
.progress-row{padding:8px 12px;background:#0f172a;display:flex;gap:10px;align-items:center;flex-shrink:0;border-top:1px solid #374151}
.progress-row .lbl{font-size:12px;color:#9ca3af;white-space:nowrap}
.progress-row .cursor-info{font-size:13px;color:#fbbf24;font-weight:bold;white-space:nowrap;min-width:90px}
.bar{height:18px;background:#374151;flex:1;border-radius:9px;overflow:visible;min-width:120px;position:relative;cursor:pointer;touch-action:none;user-select:none}
.bar > #bar{height:100%;background:linear-gradient(90deg,#22c55e,#16a34a);transition:width .15s;border-radius:9px;pointer-events:none;position:absolute;top:0;left:0}
.bar .seg-cur{position:absolute;top:0;height:100%;background:rgba(251,191,36,0.3);pointer-events:none;border-radius:3px}
.bar .marker{position:absolute;top:-4px;width:8px;height:26px;background:#fbbf24;border-radius:4px;transform:translateX(-50%);transition:left .1s;box-shadow:0 2px 6px rgba(0,0,0,0.5);pointer-events:none;z-index:2}
.bar:hover{background:#4b5563}
.bar:active{background:#6b7280}
.cursor-info{font-size:13px;color:#fbbf24;font-weight:bold;padding:0 6px;white-space:nowrap}

.explain{background:#1f2937;border-top:1px solid #374151;overflow-y:auto;padding:6px 12px;
  display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;
  grid-template-areas:"out hdr" "out input"}
.explain .explain-hdr{grid-area:hdr;display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.explain h3{font-size:12px;color:#9ca3af;margin:0;text-transform:uppercase;letter-spacing:1px;flex:1;min-width:80px}
.explain .explain-hdr button{padding:6px 12px;background:#1e3a8a;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;flex-shrink:0}
.explain textarea{grid-area:input;width:100%;min-height:54px;height:54px;padding:8px 10px;background:#0f172a;color:#f3f4f6;border:1px solid #374151;border-radius:6px;font-size:14px;font-family:inherit;resize:vertical;line-height:1.5}
.explain.has-result textarea{display:none}
/* 모바일도 PC와 동일: 좌(결과) | 우(헤더+입력) 2열 유지 */
.explain .row{display:flex;gap:6px;margin-top:6px}
.explain .row button{padding:8px 12px;background:#1e3a8a;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer}
.explain .out{grid-area:out;margin-top:0;font-size:14px;color:#e5e7eb;line-height:1.7;background:#0f172a;padding:8px;border-radius:6px;display:none;align-self:stretch;height:100%;max-height:26vh;overflow-y:auto}
@media(max-width:760px){.main #explain.has-result{max-height:24vh}.explain .out{max-height:22vh}}
.explain .out.show{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px}
@media(max-width:760px){.explain .out.show{grid-template-columns:1fr;gap:4px 8px}}
.explain .out .row-w{margin:0;display:flex;gap:10px;align-items:baseline;border-bottom:1px solid #1f2937;padding:4px 0}
@media(max-width:720px){.explain .out.show{grid-template-columns:1fr}}
.explain .out .ch{font-size:22px;color:#fbbf24;font-weight:bold;min-width:32px}
.explain .out .py{color:#16a085;font-style:italic;font-size:13px;min-width:60px}
.explain .out .ko-sound{color:#60a5fa;font-weight:bold;min-width:32px}
.explain .out .meaning{color:#e5e7eb;flex:1}
.explain .out .unknown{color:#6b7280;font-style:italic}
.explain .out .word-row{background:#1e3a8a;padding:8px;border-radius:6px;margin-bottom:6px}
.explain .out .word-row .ch{font-size:18px}

.list{flex:1;overflow-y:auto;background:#111827}
.list-row{padding:8px 12px;border-bottom:1px solid #374151;cursor:pointer;display:flex;gap:10px;font-size:13px}
.list-row:active{background:#374151}
.list-row.cur{background:#1e3a8a;color:#fff}
.list-row .ix{color:#9ca3af;width:42px;text-align:right;flex-shrink:0;font-size:11px}
.list-row .tm{color:#9ca3af;font-size:11px;width:80px;flex-shrink:0}
.list-row.cur .ix,.list-row.cur .tm{color:#bfdbfe}

@media (max-width:700px){
  .sidebar{position:fixed;right:0;top:0;bottom:0;z-index:50;width:80vw;max-width:320px;box-shadow:-2px 0 8px rgba(0,0,0,0.4)}
  .sidebar.collapsed{width:0;box-shadow:none}
  .video-wrap{max-height:40vh}
}

/* Modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,0.7);display:none;justify-content:center;align-items:center;z-index:100;padding:14px}
.modal-bg.show{display:flex}
.modal{background:#1f2937;padding:18px;border-radius:12px;max-width:480px;width:100%;max-height:90vh;overflow-y:auto}
.modal h2{font-size:16px;margin-bottom:12px;color:#bfdbfe}
.modal .row{display:flex;gap:6px;margin-bottom:8px;align-items:center;flex-wrap:wrap}
.modal .row label{font-size:12px;color:#9ca3af;min-width:80px}
.modal input[type=text],.modal input[type=number],.modal select{
  flex:1;padding:10px;font-size:14px;background:#374151;color:#fff;border:1px solid #4b5563;border-radius:6px
}
.modal button{padding:11px 16px;border:none;border-radius:6px;font-size:14px;cursor:pointer;font-weight:bold}
.modal .btn-go{background:#22c55e;color:#fff;flex:1}
.modal .btn-cancel{background:#6b7280;color:#fff}
.modal .btn-paste{background:#3b82f6;color:#fff;padding:10px 12px}
.adv{margin-top:6px;color:#9ca3af;font-size:12px;cursor:pointer}
.adv-content{display:none;margin-top:6px}
.adv-content.show{display:block}
.modal .log-box{background:#0f172a;color:#86efac;font-family:Consolas,monospace;font-size:11px;
  padding:8px;border-radius:6px;max-height:180px;overflow-y:auto;white-space:pre-wrap;margin-top:8px}
.status-msg{font-size:14px;color:#bfdbfe;margin-top:6px;font-weight:bold}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>📺</h1>
    <select class="video-select" id="vid-select"><option value="">영상 선택...</option></select>
    <button class="btn-add" onclick="openModal()">+ 추가</button>
    <div class="settings-wrap">
      <button class="btn-icon" id="btn-settings" onclick="toggleSettingsMenu(event)" title="설정">설정</button>
      <div class="settings-menu" id="settings-menu">
        <button onclick="renameProject();closeSettingsMenu()">이름 변경</button>
        <div id="menu-mode-section" style="display:none">
          <div class="menu-section">학습 모드</div>
          <button id="menu-mode-download" onclick="setMode('download');closeSettingsMenu()">📁 다운로드</button>
          <button id="menu-mode-stream" onclick="setMode('stream');closeSettingsMenu()">스트리밍</button>
        </div>
        <div class="menu-section">AI 연결 (선택)</div>
        <div id="ai-status" style="padding:6px 14px;font-size:12px;color:#9ca3af">미연결</div>
        <button onclick="setApiKey();closeSettingsMenu()">OpenRouter 키 입력</button>
        <div class="menu-section">위험</div>
        <button onclick="deleteProject();closeSettingsMenu()" style="color:#fca5a5">영상 삭제</button>
      </div>
    </div>
    <button class="btn-icon" id="btn-list-toggle" onclick="toggleSidebar()" title="문장 리스트">리스트</button>
    <button class="btn-icon" id="btn-explain-toggle" onclick="toggleExplain()" title="한자 해석">해석</button>
    <span class="stats" id="stats"></span>
  </header>

  <div class="body">
    <div class="main">
      <div class="home-screen" id="home-screen">
        <div class="hero">
          <div class="hero-logo">🔁</div>
          <h1 class="hero-title">동영상 반복하기</h1>
          <p class="hero-sub">언어 학습 영상을 자동으로 문장 단위 분리 → 무한 반복 재생</p>
        </div>
        <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
          <div class="big">파일</div>
          <div><b>로컬 영상 파일 드래그</b> 하거나 <b>클릭하여 선택</b></div>
          <div class="hint">mp4, mkv, webm, mov 등 · 자동으로 처리 시작</div>
        </div>
        <div style="text-align:center;color:#6b7280;font-size:13px;margin:8px 0 14px">
          또는 <b>+ 추가</b> 버튼으로 YouTube URL 입력
        </div>
        <input type="file" id="file-input" accept="video/*" style="display:none">
        <h2 class="section-title">내 영상 목록</h2>
        <div class="proj-grid" id="proj-grid"></div>
      </div>
      <div class="video-wrap" id="video-wrap" style="display:none">
        <div class="empty-video" id="empty-msg" style="display:none">영상을 선택하세요</div>
        <video id="v" playsinline preload="auto" style="display:none" onclick="togglePlay()"></video>
        <div id="yt-iframe" style="display:none"></div>
      </div>

      <div class="progress-row" id="progress-row" style="display:none">
        <span class="cursor-info" id="cursor-info">-</span>
        <div class="bar"><div id="bar" style="width:0%"></div><div class="seg-cur" id="seg-cur" style="display:none"></div><div class="marker" id="marker" style="left:0%"></div></div>
      </div>

      <div class="controls" id="controls" style="display:none">
        <div class="ctrl-row">
          <div class="ctrl-spacer"></div>
          <button id="prev" onclick="prevSeg()">이전</button>
          <button id="play" onclick="togglePlay()" class="play-big">▶ 재생</button>
          <button id="next" onclick="nextSeg()">다음</button>
          <div class="ctrl-spacer"></div>
          <select id="repeat">
            <option value="1" selected>반복 없음</option><option value="2">반복 2x</option>
            <option value="3">반복 3x</option><option value="4">반복 4x</option>
            <option value="5">반복 5x</option><option value="99">반복 ∞</option>
          </select>
          <button id="capture-btn" onclick="captureAndAnalyze()" class="ctrl-capture">자막 캡처</button>
          <button id="auto" class="active" onclick="toggleAuto()">자동 ON</button>
        </div>
      </div>

      <div class="explain" id="explain" style="display:none">
        <div class="explain-hdr">
          <h3>한자 해석</h3>
          <input type="file" id="img-input" accept="image/*" style="display:none" onchange="handleImageFile(this.files[0])">
          <button onclick="analyzeChinese()" style="background:#22c55e">사전</button>
          <button onclick="aiAnalyzeText()" id="ai-btn" style="background:#f59e0b;display:none">AI</button>
          <button onclick="lookupExternal()" style="background:#0ea5e9">네이버</button>
          <button onclick="clearExplain()" style="background:#6b7280">지우기</button>
        </div>
        <textarea id="explain-input" rows="2" placeholder="자막 캡처 또는 직접 입력 → 사전 / 네이버"></textarea>
        <div class="out show" id="explain-out" style="min-height:100px"><div style="color:#6b7280;text-align:center;padding:20px">위 입력란에 한자/일본어를 입력하고 <b>분석</b> 클릭하면<br>여기에 글자별 음/뜻이 표시됩니다.</div></div>
      </div>
    </div>

    <div class="sidebar collapsed" id="sidebar">
      <div class="sidebar-hdr">
        <h3>문장 리스트</h3>
        <button class="btn-icon" onclick="toggleSidebar()" style="padding:4px 8px">×</button>
      </div>
      <div class="list" id="list"></div>
    </div>
  </div>
</div>

<!-- Add modal -->
<div class="drop-overlay" id="drop-overlay">영상 파일 놓으세요</div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <h2>새 영상 처리</h2>
    <div style="background:#1e293b;border-left:3px solid #f59e0b;padding:8px 12px;margin-bottom:10px;font-size:12px;color:#cbd5e1;border-radius:4px">
      안내: 본인 권한이 있는 영상이거나 사적 학습용으로만 사용하세요. 저작권/YouTube 약관 준수는 사용자 책임입니다.
    </div>
    <div class="row">
      <input type="text" id="m-url" placeholder="https://www.youtube.com/watch?v=...">
      <button class="btn-paste" onclick="pasteUrl()">붙여넣기</button>
    </div>
    <div class="row">
      <input type="text" id="m-pid" placeholder="프로젝트 ID (영문/숫자)">
    </div>
    <div class="adv" onclick="document.getElementById('adv-content').classList.toggle('show')">고급</div>
    <div class="adv-content" id="adv-content">
      <div class="row">
        <label>모드</label>
        <select id="m-mode">
          <option value="both" selected>다운+스트림</option>
          <option value="download">다운만</option>
          <option value="stream">스트림만</option>
        </select>
      </div>
      <div class="row">
        <label>무음 dB</label>
        <select id="m-noise">
          <option value="-25">-25</option><option value="-30" selected>-30</option>
          <option value="-35">-35</option><option value="-40">-40</option>
        </select>
      </div>
      <div class="row">
        <label>최소 무음(s)</label>
        <select id="m-silence">
          <option value="0.2">0.2</option><option value="0.3">0.3</option>
          <option value="0.4" selected>0.4</option><option value="0.7">0.7</option>
        </select>
      </div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn-go" onclick="startProcess()">처리 시작</button>
      <button class="btn-cancel" onclick="closeModal()">취소</button>
    </div>
    <div class="status-msg" id="m-status"></div>
    <div class="log-box" id="m-log" style="display:none"></div>
  </div>
</div>

<script src="https://www.youtube.com/iframe_api"></script>
<script>
let curMeta = null;
let SEGMENTS = [];
let cur = 0, count = 0, autoNext = true;
let mode = 'download';
let ytPlayer = null, ytTimer = null;

const v = document.getElementById('v');
const ytDiv = document.getElementById('yt-iframe');

function fmt(t){ const m=Math.floor(t/60),s=Math.floor(t%60); return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`; }

async function loadProjects(){
  try {
    const r = await fetch('/api/list');
    const data = await r.json();
    const sel = document.getElementById('vid-select');
    const curPid = sel.value;
    sel.innerHTML = '<option value="">영상 선택...</option>' + data.map(p =>
      `<option value="${p.id}">${p.name||p.id} (${p.segments_count}개${p.has_video?', 저장':''})</option>`
    ).join('');
    if(curPid && data.some(p => p.id === curPid)) sel.value = curPid;
    // Render home grid too
    const grid = document.getElementById('proj-grid');
    if(grid){
      if(!data.length){
        grid.innerHTML = '<div class="empty">아직 영상이 없습니다. 우측 상단 + 추가 버튼을 누르세요.</div>';
      } else {
        grid.innerHTML = data.map(p => `
          <div class="proj-card" data-pid="${p.id}">
            <div class="actions">
              <button class="btn-rename" data-pid="${p.id}" title="이름 변경">이름</button>
              <button class="btn-delete" data-pid="${p.id}" title="삭제">삭제</button>
            </div>
            <div class="name">📺 ${p.name||p.id}</div>
            <div class="id">ID: ${p.id}</div>
            <div class="meta">${p.segments_count}개 문장 · ${p.size_mb}MB ${p.has_video?'[저장]':''}${p.has_stream?' [스트림]':''}</div>
          </div>`).join('');
        grid.querySelectorAll('.proj-card').forEach(c => {
          c.addEventListener('click', () => openProject(c.dataset.pid));
        });
        grid.querySelectorAll('.btn-rename').forEach(b => {
          b.addEventListener('click', e => { e.stopPropagation(); renameById(b.dataset.pid); });
        });
        grid.querySelectorAll('.btn-delete').forEach(b => {
          b.addEventListener('click', e => { e.stopPropagation(); deleteById(b.dataset.pid); });
        });
      }
    }
  } catch(e){ console.error(e); }
}

function openProject(pid){
  const sel = document.getElementById('vid-select');
  sel.value = pid;
  sel.dispatchEvent(new Event('change'));
}
async function renameById(pid){
  const cur_name = (await (await fetch('/api/list')).json()).find(p=>p.id===pid)?.name || pid;
  const newName = prompt('새 이름:', cur_name);
  if(!newName || newName === cur_name) return;
  await fetch('/api/rename', {method:'POST', body: new URLSearchParams({pid, name: newName})});
  loadProjects();
}
async function deleteById(pid){
  if(!confirm(`"${pid}" 삭제? 영상 파일까지 지워집니다.`)) return;
  await fetch('/api/delete', {method:'POST', body: new URLSearchParams({pid})});
  if(curMeta && curMeta.id === pid) closePlayer();
  loadProjects();
}

async function renameProject(){
  const sel = document.getElementById('vid-select');
  const pid = sel.value;
  if(!pid){ alert('먼저 영상을 선택하세요'); return; }
  const cur_name = sel.options[sel.selectedIndex].text.split(' (')[0];
  const newName = prompt('새 이름 (한글 가능):', cur_name);
  if(!newName || newName === cur_name) return;
  try {
    const body = new URLSearchParams({pid, name: newName});
    const r = await fetch('/api/rename', {method:'POST', body});
    const data = await r.json();
    if(data.error){ alert('오류: '+data.error); return; }
    await loadProjects();
    sel.value = pid;
  } catch(e){ alert(e.message); }
}

async function deleteProject(){
  const sel = document.getElementById('vid-select');
  const pid = sel.value;
  if(!pid){ alert('먼저 영상을 선택하세요'); return; }
  if(!confirm(`정말 "${pid}"를 삭제할까요? 영상 파일까지 모두 지웁니다.`)) return;
  try {
    const body = new URLSearchParams({pid});
    const r = await fetch('/api/delete', {method:'POST', body});
    const data = await r.json();
    if(data.error){ alert('오류: '+data.error); return; }
    closePlayer();
    await loadProjects();
    sel.value = '';
  } catch(e){ alert(e.message); }
}

document.getElementById('vid-select').addEventListener('change', async e => {
  const pid = e.target.value;
  if(!pid){ closePlayer(); return; }
  await loadProject(pid);
});

async function loadProject(pid){
  try {
    const r = await fetch('/api/meta?pid=' + encodeURIComponent(pid));
    const meta = await r.json();
    if(!meta || !meta.segments){ alert('메타 로드 실패'); return; }
    curMeta = meta;
    SEGMENTS = meta.segments;
    cur = 0; count = 0;
    setupModeToggle();
    setMode(meta.has_download ? 'download' : 'stream');
    document.getElementById('home-screen').style.display = 'none';
    document.getElementById('video-wrap').style.display = 'flex';
    document.getElementById('controls').style.display = 'flex';
    document.getElementById('progress-row').style.display = 'flex';
    document.getElementById('explain').style.display = 'block';
    document.getElementById('empty-msg').style.display = 'none';
    renderList();
    updateStats();
    if(mode === 'download'){
      v.src = '/' + meta.video_file;
      v.load();
    }
  } catch(e){ console.error(e); alert('오류: ' + e.message); }
}

function closePlayer(){
  v.style.display = 'none'; ytDiv.style.display = 'none';
  document.getElementById('controls').style.display = 'none';
  document.getElementById('progress-row').style.display = 'none';
  document.getElementById('explain').style.display = 'none';
  document.getElementById('sidebar').classList.add('collapsed');
  document.getElementById('video-wrap').style.display = 'none';
  document.getElementById('home-screen').style.display = 'block';
  curMeta = null;
  document.getElementById('vid-select').value = '';
  v.pause(); v.src = '';
  if(ytPlayer && ytPlayer.destroy){ ytPlayer.destroy(); ytPlayer = null; }
  if(ytTimer){ clearInterval(ytTimer); ytTimer = null; }
}

function toggleSidebar(){
  document.getElementById('sidebar').classList.toggle('collapsed');
}
function toggleExplain(){
  const el = document.getElementById('explain');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
function lookupSelection(){
  const ta = document.getElementById('explain-input');
  const text = ta.value.substring(ta.selectionStart, ta.selectionEnd).trim() || ta.value.trim();
  if(!text){ alert('단어를 선택하거나 입력하세요'); return; }
  window.open('https://krdict.korean.go.kr/kor/dicSearch/search?nation=zh&nationCode=&ParaWordNo=&mainSearchWord=' + encodeURIComponent(text), '_blank');
}
function copyExplain(){
  const ta = document.getElementById('explain-input');
  ta.select();
  document.execCommand('copy');
}
function clearExplain(){
  document.getElementById('explain-input').value = '';
  document.getElementById('explain-out').classList.remove('show');
  document.getElementById('explain').classList.remove('has-result');
  const k = analysisKey(cur);
  if(k){ try{ localStorage.removeItem(k); }catch(e){} }
}
function markHasResult(){
  document.getElementById('explain').classList.add('has-result');
  saveAnalysis();
}

function setupModeToggle(){
  // Mode toggle is now inside settings menu - show only if both modes available
  const section = document.getElementById('menu-mode-section');
  if(curMeta && curMeta.has_download && curMeta.has_stream && curMeta.video_id){
    section.style.display = 'block';
    document.getElementById('menu-mode-download').classList.toggle('active', mode === 'download');
    document.getElementById('menu-mode-stream').classList.toggle('active', mode === 'stream');
  } else {
    section.style.display = 'none';
  }
}
function toggleSettingsMenu(e){
  if(e) e.stopPropagation();
  document.getElementById('settings-menu').classList.toggle('show');
}
function closeSettingsMenu(){
  document.getElementById('settings-menu').classList.remove('show');
}
document.addEventListener('click', e => {
  if(!e.target.closest('#settings-menu') && !e.target.closest('#btn-settings')){
    closeSettingsMenu();
  }
});

function setMode(newMode){
  mode = newMode;
  if(mode === 'download'){
    v.style.display = 'block';
    ytDiv.style.display = 'none';
    if(ytPlayer && ytPlayer.pauseVideo) ytPlayer.pauseVideo();
    if(curMeta && v.src !== location.origin + '/' + curMeta.video_file){
      v.src = '/' + curMeta.video_file;
    }
  } else {
    v.style.display = 'none';
    ytDiv.style.display = 'block';
    v.pause();
    if(!ytPlayer && curMeta.video_id){ initYouTube(); }
  }
}

function initYouTube(){
  if(ytPlayer) ytPlayer.destroy();
  ytPlayer = new YT.Player('yt-iframe', {
    videoId: curMeta.video_id,
    playerVars: {playsinline:1, modestbranding:1, controls:1, rel:0},
    events: {'onReady': () => { ytPlayer.seekTo(SEGMENTS[cur].start, true); startYtMonitor(); }}
  });
}
function startYtMonitor(){
  if(ytTimer) clearInterval(ytTimer);
  ytTimer = setInterval(() => {
    if(!ytPlayer || !ytPlayer.getCurrentTime || mode !== 'stream' || cur >= SEGMENTS.length) return;
    const t = ytPlayer.getCurrentTime();
    if(t >= SEGMENTS[cur].end - 0.05) onSegmentEnd();
  }, 200);
}
function onYouTubeIframeAPIReady(){}

function getCurrentTime(){ return mode === 'download' ? v.currentTime : (ytPlayer&&ytPlayer.getCurrentTime?ytPlayer.getCurrentTime():0); }
let ourSeekTarget = null;
function seekTo(t){
  if(mode === 'download'){
    ourSeekTarget = t;
    v.currentTime = t;
  } else if(ytPlayer&&ytPlayer.seekTo){
    ytPlayer.seekTo(t, true);
  }
}
function play(){ if(mode === 'download'){ v.play(); } else if(ytPlayer&&ytPlayer.playVideo){ ytPlayer.playVideo(); } }
function pause(){ if(mode === 'download'){ v.pause(); } else if(ytPlayer&&ytPlayer.pauseVideo){ ytPlayer.pauseVideo(); } }

function jumpToCur(){
  console.log('[jumpToCur] cur=', cur, 'SEGMENTS.length=', SEGMENTS.length);
  if(cur < 0) cur = 0;
  if(cur >= SEGMENTS.length){ pause(); return; }
  console.log('[jumpToCur] seekTo', SEGMENTS[cur].start);
  seekTo(SEGMENTS[cur].start); play();
  updateStats(); renderList();
  loadAnalysisForSegment(cur);
}

// === 세그먼트별 분석 결과 캐시 ===
function analysisKey(idx){ return curMeta ? `analysis:${curMeta.id}:${idx}` : null; }
function saveAnalysis(){
  const k = analysisKey(cur); if(!k) return;
  const text = document.getElementById('explain-input').value || '';
  const out = document.getElementById('explain-out').innerHTML || '';
  const hasResult = document.getElementById('explain').classList.contains('has-result');
  if(!text && !hasResult){ try{ localStorage.removeItem(k); }catch(e){} return; }
  try{ localStorage.setItem(k, JSON.stringify({text, out, hasResult, ts:Date.now()})); }catch(e){}
}
function loadAnalysisForSegment(idx){
  const k = curMeta ? `analysis:${curMeta.id}:${idx}` : null;
  if(!k){ return; }
  let saved = null;
  try{ saved = JSON.parse(localStorage.getItem(k) || 'null'); }catch(e){}
  const inp = document.getElementById('explain-input');
  const out = document.getElementById('explain-out');
  const exp = document.getElementById('explain');
  if(saved){
    inp.value = saved.text || '';
    out.innerHTML = saved.out || '';
    if(saved.hasResult){ out.classList.add('show'); exp.classList.add('has-result'); }
    else { out.classList.remove('show'); exp.classList.remove('has-result'); }
  } else {
    inp.value = '';
    out.innerHTML = '<div style="color:#6b7280;text-align:center;padding:20px">위 입력란에 한자/일본어를 입력하고 <b>분석</b> 클릭하면<br>여기에 글자별 음/뜻이 표시됩니다.</div>';
    out.classList.add('show');
    exp.classList.remove('has-result');
  }
}
function onSegmentEnd(){
  if(cur >= SEGMENTS.length) return;
  console.log('[segEnd] cur=', cur, 'time=', getCurrentTime().toFixed(1), 'seg.end=', SEGMENTS[cur].end);
  count++;
  const repeats = parseInt(document.getElementById('repeat').value);
  console.log('[segEnd] count=', count, 'repeats=', repeats);
  if(count < repeats){
    console.log('[segEnd] repeating: seekTo', SEGMENTS[cur].start);
    seekTo(SEGMENTS[cur].start);
  } else {
    count = 0;
    if(autoNext){
      console.log('[segEnd] advancing to seg', cur+1);
      pause(); cur++; jumpToCur();
    }
    else { pause(); }
  }
  updateStats();
}

// Find segment that contains time t (or 'best fit' = previous one)
function findSegmentAt(t){
  let best = 0;
  for(let i=0; i<SEGMENTS.length; i++){
    if(SEGMENTS[i].start <= t && t < SEGMENTS[i].end){ return i; }
    if(t >= SEGMENTS[i].start) best = i;
  }
  return best;
}

let isSeeking = false;
v.addEventListener('seeking', () => { isSeeking = true; });
v.addEventListener('seeked', () => {
  isSeeking = false;
  const t = v.currentTime;
  // If this was our own seek and target wasn't reached → seek failed (file unseekable)
  if(ourSeekTarget !== null){
    if(Math.abs(t - ourSeekTarget) > 1.0){
      console.warn('[seeked] SEEK FAILED: target=', ourSeekTarget, 'actual=', t, '— pausing to avoid infinite loop');
      pause();
      ourSeekTarget = null;
      // Show a warning to user
      const ci = document.getElementById('cursor-info');
      if(ci) ci.textContent = '시킹 실패 - 영상 파일 문제';
      return;
    }
    ourSeekTarget = null;
    return;  // our seek succeeded, no need to sync cur
  }
  // User scrub: sync cur to where they jumped
  const found = findSegmentAt(t);
  console.log('[seeked] user scrub t=', t.toFixed(1), 'cur was', cur, 'found seg', found);
  if(found !== cur){
    cur = found; count = 0; updateStats(); renderList();
  }
});

v.addEventListener('timeupdate', () => {
  updateBar();  // smooth time-based progress tracking
  if(mode !== 'download' || cur >= SEGMENTS.length || barDragging || isSeeking) return;
  if(v.currentTime >= SEGMENTS[cur].end - 0.05) onSegmentEnd();
});

v.addEventListener('loadedmetadata', () => {
  if(SEGMENTS[0]){
    cur = 0; count = 0;
    v.currentTime = SEGMENTS[0].start;
    updateStats(); renderList();
  }
});
function saveProgress(){}  // no-op (kept for compat)

function prevSeg(){ cur = Math.max(0, cur-1); count = 0; jumpToCur(); }
function nextSeg(){ cur = Math.min(SEGMENTS.length-1, cur+1); count = 0; jumpToCur(); }
function togglePlay(){
  if(mode === 'download'){ v.paused ? play() : pause(); }
  else { const s = ytPlayer&&ytPlayer.getPlayerState?ytPlayer.getPlayerState():-1; if(s===1) pause(); else play(); }
}
function toggleAuto(){
  autoNext = !autoNext;
  document.getElementById('auto').classList.toggle('active', autoNext);
  document.getElementById('auto').textContent = autoNext ? '자동 ON' : '자동 OFF';
}

// Progress bar click + drag (event delegation - works regardless of when .bar appears)
let barDragging = false;
function pctToSeg(pct){
  pct = Math.max(0, Math.min(1, pct));
  return Math.max(0, Math.min(SEGMENTS.length - 1, Math.floor(pct * SEGMENTS.length)));
}
function getBarRect(){
  const bar = document.querySelector('.bar');
  return bar ? bar.getBoundingClientRect() : null;
}
function eventPct(e){
  const rect = getBarRect();
  if(!rect || !rect.width) return 0;
  const x = (e.touches && e.touches[0] ? e.touches[0].clientX : e.clientX) - rect.left;
  return x / rect.width;
}
function jumpToPctSeg(pct, commit){
  if(!SEGMENTS.length) return;
  const idx = pctToSeg(pct);
  cur = idx; count = 0;
  if(commit) jumpToCur(); else { updateStats(); renderList(); }
}

// Time-based bar interaction (matches native video timeline)
function pctToTime(pct){
  pct = Math.max(0, Math.min(1, pct));
  return pct * getDuration();
}
function eventBarPct(e){
  const bar = document.querySelector('.bar');
  if(!bar) return 0;
  const rect = bar.getBoundingClientRect();
  if(!rect.width) return 0;
  const x = (e.touches && e.touches[0] ? e.touches[0].clientX : e.clientX) - rect.left;
  return x / rect.width;
}
function bindBarInteraction(){
  const bar = document.querySelector('.bar');
  if(!bar) return;
  const startDrag = e => {
    if(!SEGMENTS.length) return;
    barDragging = true;
    seekTo(pctToTime(eventBarPct(e)));
    if(e.preventDefault) e.preventDefault();
  };
  bar.addEventListener('mousedown', startDrag);
  bar.addEventListener('touchstart', startDrag, {passive:false});
}
document.addEventListener('mousemove', e => {
  if(!barDragging) return;
  seekTo(pctToTime(eventBarPct(e)));
});
document.addEventListener('touchmove', e => {
  if(!barDragging) return;
  seekTo(pctToTime(eventBarPct(e)));
}, {passive:true});
document.addEventListener('mouseup', () => {
  if(!barDragging) return;
  barDragging = false;
  // seeked event will sync cur and resume play
  play();
});
document.addEventListener('touchend', () => {
  if(!barDragging) return;
  barDragging = false;
  play();
});
document.addEventListener('DOMContentLoaded', bindBarInteraction);
bindBarInteraction();

function getDuration(){
  if(mode === 'download') return v.duration || 0;
  return ytPlayer && ytPlayer.getDuration ? ytPlayer.getDuration() : 0;
}
function updateBar(){
  // Time-based bar (matches native video timeline)
  const t = getCurrentTime();
  const dur = getDuration();
  if(dur > 0){
    const pct = Math.min(100, Math.max(0, t/dur*100));
    document.getElementById('bar').style.width = pct + '%';
    document.getElementById('marker').style.left = pct + '%';
    // Highlight current segment range on bar
    if(SEGMENTS.length && cur < SEGMENTS.length){
      const seg = SEGMENTS[cur];
      const segStart = seg.start / dur * 100;
      const segWidth = (seg.end - seg.start) / dur * 100;
      const segCur = document.getElementById('seg-cur');
      segCur.style.left = segStart + '%';
      segCur.style.width = segWidth + '%';
      segCur.style.display = 'block';
    }
  }
}
function updateStats(){
  if(!SEGMENTS.length){
    document.getElementById('stats').textContent = '';
    document.getElementById('cursor-info').textContent = '-';
    document.getElementById('bar').style.width = '0%';
    document.getElementById('marker').style.left = '0%';
    return;
  }
  document.getElementById('stats').textContent = `${cur+1}/${SEGMENTS.length} (${count+1}회)`;
  const seg = SEGMENTS[cur];
  document.getElementById('cursor-info').textContent = `📍 ${cur+1}번 ${fmt(seg.start)}`;
  updateBar();
  // If segment has pre-OCR'd text, auto-fill (otherwise user clicks 캡처+분석)
  if(seg.text){
    const inp = document.getElementById('explain-input');
    if(inp && inp.value !== seg.text){
      inp.value = seg.text;
      analyzeChinese();
    }
  }
}
function renderList(){
  const list = document.getElementById('list');
  list.innerHTML = SEGMENTS.map((s,i) =>
    `<div class="list-row${i===cur?' cur':''}" data-i="${i}">
      <span class="ix">#${i+1}</span><span class="tm">${fmt(s.start)}</span>
      <span>${s.dur.toFixed(1)}s</span></div>`
  ).join('');
  list.querySelectorAll('.list-row').forEach(r => {
    r.addEventListener('click', () => {
      cur = parseInt(r.dataset.i);
      count = 0;
      jumpToCur();
    });
  });
  const curRow = list.querySelector('.list-row.cur');
  if(curRow) curRow.scrollIntoView({block:'nearest', behavior:'smooth'});
}
v.addEventListener('play', () => document.getElementById('play').textContent = '⏸');
v.addEventListener('pause', () => document.getElementById('play').textContent = '▶');

// === Add new video modal ===
function openModal(){ document.getElementById('modal').classList.add('show'); }
function closeModal(){
  document.getElementById('modal').classList.remove('show');
  document.getElementById('m-status').textContent = '';
  document.getElementById('m-log').style.display = 'none';
  document.getElementById('m-log').textContent = '';
}
// === 한자 사전 (자주 쓰는 글자 + 단어) ===
const HANJA_DICT = {
  // 인칭/지시
  '我':{s:'아',m:'나/저'},'你':{s:'니',m:'너'},'您':{s:'녕',m:'당신(존칭)'},
  '他':{s:'타',m:'그(남)'},'她':{s:'타',m:'그녀'},'它':{s:'타',m:'그것'},
  '们':{s:'문',m:'~들(복수)'},'这':{s:'저',m:'이'},'那':{s:'나',m:'그/저'},
  '什':{s:'십',m:'(什么) 무엇'},'么':{s:'마',m:'(어조사)'},'谁':{s:'수',m:'누구'},
  '哪':{s:'나',m:'어느'},'怎':{s:'즘',m:'어떻게'},'多':{s:'다',m:'많다'},
  '少':{s:'소',m:'적다'},'几':{s:'기',m:'몇'},
  // 동사 빈출
  '是':{s:'시',m:'~이다'},'有':{s:'유',m:'있다'},'没':{s:'몰',m:'없다'},
  '去':{s:'거',m:'가다'},'来':{s:'래',m:'오다'},'回':{s:'회',m:'돌아오다'},
  '走':{s:'주',m:'걷다/가다'},'跑':{s:'포',m:'뛰다'},
  '吃':{s:'끽',m:'먹다'},'喝':{s:'갈',m:'마시다'},'看':{s:'간',m:'보다'},
  '听':{s:'청',m:'듣다'},'说':{s:'설',m:'말하다'},'讲':{s:'강',m:'말하다'},
  '想':{s:'상',m:'생각하다/하고 싶다'},'要':{s:'요',m:'필요/원하다'},
  '会':{s:'회',m:'할 줄 안다/회의'},'能':{s:'능',m:'~할 수 있다'},
  '可':{s:'가',m:'가능/그러나'},'做':{s:'주',m:'하다/만들다'},'干':{s:'간',m:'하다'},
  '给':{s:'급',m:'주다'},'拿':{s:'나',m:'들다'},'送':{s:'송',m:'보내다/선물'},
  '买':{s:'매',m:'사다'},'卖':{s:'매',m:'팔다'},'帮':{s:'방',m:'돕다'},
  '开':{s:'개',m:'열다/켜다'},'关':{s:'관',m:'닫다/끄다'},'打':{s:'타',m:'치다/걸다'},
  '坐':{s:'좌',m:'앉다/타다'},'站':{s:'참',m:'서다'},'起':{s:'기',m:'일어나다'},
  '床':{s:'상',m:'침대'},'睡':{s:'수',m:'자다'},'觉':{s:'각',m:'잠/느끼다'},
  '洗':{s:'세',m:'씻다'},'刷':{s:'쇄',m:'닦다'},'擦':{s:'찰',m:'닦다'},
  '放':{s:'방',m:'놓다'},'拉':{s:'랍',m:'당기다'},'推':{s:'추',m:'밀다'},
  '找':{s:'조',m:'찾다'},'等':{s:'등',m:'기다리다'},'问':{s:'문',m:'묻다'},
  '答':{s:'답',m:'답하다'},'懂':{s:'동',m:'이해하다'},'知':{s:'지',m:'알다'},
  '学':{s:'학',m:'배우다'},'习':{s:'습',m:'익히다'},'教':{s:'교',m:'가르치다'},
  '工':{s:'공',m:'일'},'作':{s:'작',m:'짓다/일'},'班':{s:'반',m:'반/근무'},
  '休':{s:'휴',m:'쉬다'},'息':{s:'식',m:'쉬다'},
  '喜':{s:'희',m:'기쁘다'},'欢':{s:'환',m:'기쁘다'},'爱':{s:'애',m:'사랑'},
  '讨':{s:'토',m:'(讨厌) 싫어하다'},'厌':{s:'염',m:'싫다'},'怕':{s:'파',m:'무섭다'},
  '担':{s:'담',m:'(担心) 걱정'},'心':{s:'심',m:'마음/심장'},'念':{s:'념',m:'생각하다'},
  '高':{s:'고',m:'높다'},'兴':{s:'흥',m:'(高兴) 기쁘다'},'伤':{s:'상',m:'다치다'},
  '希':{s:'희',m:'(希望) 바라다'},'望':{s:'망',m:'바라보다'},'决':{s:'결',m:'결정'},
  '记':{s:'기',m:'기억하다'},'忘':{s:'망',m:'잊다'},'试':{s:'시',m:'시도'},
  '换':{s:'환',m:'바꾸다'},'修':{s:'수',m:'수리'},'坏':{s:'괴',m:'고장'},
  '掉':{s:'도',m:'떨어지다'},'丢':{s:'주',m:'잃다'},'剪':{s:'전',m:'자르다'},
  // 시간
  '今':{s:'금',m:'(今天) 오늘'},'天':{s:'천',m:'하늘/날'},'明':{s:'명',m:'(明天) 내일'},
  '昨':{s:'작',m:'어제'},'后':{s:'후',m:'뒤'},'前':{s:'전',m:'앞'},
  '现':{s:'현',m:'(现在) 지금'},'在':{s:'재',m:'~에 있다'},'以':{s:'이',m:'~로'},
  '早':{s:'조',m:'아침/이르다'},'上':{s:'상',m:'위/오르다'},'午':{s:'오',m:'낮'},
  '中':{s:'중',m:'가운데'},'下':{s:'하',m:'아래'},'晚':{s:'만',m:'늦다/밤'},
  '年':{s:'년',m:'해'},'月':{s:'월',m:'달'},'日':{s:'일',m:'날'},'号':{s:'호',m:'호수'},
  '周':{s:'주',m:'주(week)'},'末':{s:'말',m:'끝'},'星':{s:'성',m:'별'},'期':{s:'기',m:'기간'},
  '点':{s:'점',m:'점/시(시간)'},'分':{s:'분',m:'나누다/분'},'秒':{s:'초',m:'초'},
  '小':{s:'소',m:'작다'},'时':{s:'시',m:'때'},'刻':{s:'각',m:'시각'},
  '一':{s:'일',m:'하나'},'下':{s:'하',m:'아래/잠깐'},'马':{s:'마',m:'말'},
  '已':{s:'이',m:'이미'},'经':{s:'경',m:'거치다'},'正':{s:'정',m:'바르다'},
  '刚':{s:'강',m:'방금'},'才':{s:'재',m:'겨우'},'就':{s:'취',m:'바로'},
  '快':{s:'쾌',m:'빠르다'},'慢':{s:'만',m:'느리다'},'总':{s:'총',m:'항상'},
  // 장소/사물
  '家':{s:'가',m:'집'},'校':{s:'교',m:'(学校) 학교'},'公':{s:'공',m:'(公司) 회사'},
  '司':{s:'사',m:'맡다'},'医':{s:'의',m:'의사'},'院':{s:'원',m:'기관'},
  '餐':{s:'찬',m:'식사'},'厅':{s:'청',m:'홀'},'店':{s:'점',m:'가게'},
  '超':{s:'초',m:'초월'},'市':{s:'시',m:'시장'},'银':{s:'은',m:'은(銀)'},'行':{s:'행',m:'다니다'},
  '机':{s:'기',m:'기계'},'场':{s:'장',m:'장소'},'车':{s:'차',m:'차'},'站':{s:'참',m:'역'},
  '地':{s:'지',m:'땅'},'铁':{s:'철',m:'쇠'},'路':{s:'로',m:'길'},'口':{s:'구',m:'입'},
  '房':{s:'방',m:'방'},'间':{s:'간',m:'사이'},'门':{s:'문',m:'문'},'窗':{s:'창',m:'창'},
  '楼':{s:'루',m:'층/건물'},'梯':{s:'제',m:'사다리'},'电':{s:'전',m:'전기'},
  '视':{s:'시',m:'보다'},'话':{s:'화',m:'말'},'脑':{s:'뇌',m:'뇌'},'机':{s:'기',m:'기계'},
  '手':{s:'수',m:'손'},'书':{s:'서',m:'책'},'报':{s:'보',m:'알리다'},'纸':{s:'지',m:'종이'},
  '钱':{s:'전',m:'돈'},'卡':{s:'카',m:'카드'},'票':{s:'표',m:'표'},'信':{s:'신',m:'편지/믿다'},
  '床':{s:'상',m:'침대'},'桌':{s:'탁',m:'책상'},'椅':{s:'의',m:'의자'},'子':{s:'자',m:'아이/접미사'},
  '衣':{s:'의',m:'옷'},'服':{s:'복',m:'옷'},'裤':{s:'고',m:'바지'},'鞋':{s:'혜',m:'신발'},
  '包':{s:'포',m:'가방'},'伞':{s:'산',m:'우산'},'钥':{s:'약',m:'(钥匙) 열쇠'},'匙':{s:'시',m:'숟가락'},
  '风':{s:'풍',m:'바람'},'扇':{s:'선',m:'부채'},'空':{s:'공',m:'(空调) 에어컨/비다'},'调':{s:'조',m:'고르다'},
  '冰':{s:'빙',m:'얼음'},'箱':{s:'상',m:'상자'},'微':{s:'미',m:'작다'},'波':{s:'파',m:'물결'},'炉':{s:'로',m:'난로'},
  '充':{s:'충',m:'충전'},'器':{s:'기',m:'그릇/기계'},'牙':{s:'아',m:'이(牙)'},'刷':{s:'쇄',m:'칫솔'},'膏':{s:'고',m:'(牙膏) 치약'},
  '咖':{s:'가',m:'(咖啡) 커피'},'啡':{s:'비',m:'커피'},'茶':{s:'다',m:'차'},'水':{s:'수',m:'물'},
  '饭':{s:'반',m:'밥'},'米':{s:'미',m:'쌀'},'面':{s:'면',m:'얼굴/국수'},'包':{s:'포',m:'(面包) 빵'},
  '肉':{s:'육',m:'고기'},'鱼':{s:'어',m:'생선'},'蛋':{s:'단',m:'알'},'鸡':{s:'계',m:'닭'},
  '菜':{s:'채',m:'채소/요리'},'果':{s:'과',m:'과일'},'苹':{s:'평',m:'(苹果) 사과'},'橘':{s:'귤',m:'귤'},
  '酒':{s:'주',m:'술'},'啤':{s:'비',m:'(啤酒) 맥주'},
  '盐':{s:'염',m:'소금'},'糖':{s:'당',m:'설탕'},'醋':{s:'초',m:'식초'},
  '碟':{s:'접',m:'(碟子) 접시'},'杯':{s:'배',m:'잔'},'筷':{s:'쾌',m:'(筷子) 젓가락'},
  '药':{s:'약',m:'약'},'冒':{s:'모',m:'(感冒) 감기'},'感':{s:'감',m:'느끼다'},
  '雨':{s:'우',m:'비'},'雪':{s:'설',m:'눈'},'热':{s:'열',m:'덥다'},'冷':{s:'랭',m:'춥다'},
  // 형용사
  '好':{s:'호',m:'좋다'},'大':{s:'대',m:'크다'},'新':{s:'신',m:'새것'},'旧':{s:'구',m:'헌'},
  '远':{s:'원',m:'멀다'},'近':{s:'근',m:'가깝다'},'老':{s:'로',m:'늙다'},'轻':{s:'경',m:'가볍다'},
  '漂':{s:'표',m:'(漂亮) 예쁘다'},'亮':{s:'량',m:'밝다'},'帅':{s:'수',m:'잘생기다'},
  '难':{s:'난',m:'어렵다'},'容':{s:'용',m:'(容易) 쉽다'},'易':{s:'이',m:'쉽다'},
  '便':{s:'편',m:'(便宜) 싸다'},'宜':{s:'의',m:'마땅'},'贵':{s:'귀',m:'비싸다'},
  '干':{s:'간',m:'마르다/깨끗'},'净':{s:'정',m:'깨끗'},'脏':{s:'장',m:'더럽다'},
  '安':{s:'안',m:'(安静) 조용'},'静':{s:'정',m:'고요'},'闹':{s:'료',m:'시끄럽다'},
  '忙':{s:'망',m:'바쁘다'},'累':{s:'루',m:'피곤'},'舒':{s:'서',m:'(舒服) 편안'},'服':{s:'복',m:'옷'},
  '饿':{s:'아',m:'배고프다'},'饱':{s:'포',m:'배부르다'},'渴':{s:'갈',m:'목마르다'},
  '困':{s:'곤',m:'졸리다'},'急':{s:'급',m:'급하다'},
  // 부사/조사
  '不':{s:'불',m:'안~'},'别':{s:'별',m:'~하지 마라'},'很':{s:'흔',m:'매우'},'太':{s:'태',m:'너무'},
  '真':{s:'진',m:'정말'},'非':{s:'비',m:'(非常) 대단히'},'常':{s:'상',m:'늘'},'特':{s:'특',m:'특별히'},
  '都':{s:'도',m:'모두'},'也':{s:'야',m:'~도'},'还':{s:'환',m:'아직/또'},'又':{s:'우',m:'또'},
  '只':{s:'지',m:'오직'},'更':{s:'경',m:'더'},'最':{s:'최',m:'가장'},'比':{s:'비',m:'~보다'},
  '所':{s:'소',m:'(所以) 그래서'},'但':{s:'단',m:'(但是) 그러나'},'因':{s:'인',m:'(因为) 때문'},
  '为':{s:'위',m:'위하다'},'如':{s:'여',m:'같다'},'果':{s:'과',m:'(如果) 만약'},
  '虽':{s:'수',m:'(虽然) 비록'},'然':{s:'연',m:'그러하다'},'而':{s:'이',m:'그러나'},'且':{s:'차',m:'또한'},
  '的':{s:'적',m:'~의 (어조사)'},'了':{s:'료',m:'(완료/변화)'},'吗':{s:'마',m:'~까?'},'吧':{s:'파',m:'~죠'},
  '呢':{s:'니',m:'~는?'},'啊':{s:'아',m:'~아'},'过':{s:'과',m:'~한 적'},'着':{s:'착',m:'~고 있다'},
  '从':{s:'종',m:'~부터'},'到':{s:'도',m:'~까지'},'跟':{s:'근',m:'~와'},'和':{s:'화',m:'~와'},
  '把':{s:'파',m:'(처치형)'},'让':{s:'양',m:'~하게 하다'},'被':{s:'피',m:'~당하다(피동)'},
  '一':{s:'일',m:'하나'},'些':{s:'사',m:'(一些) 약간'},'儿':{s:'아',m:'아이/접미사'},
  // 숫자
  '二':{s:'이',m:'2'},'三':{s:'삼',m:'3'},'四':{s:'사',m:'4'},'五':{s:'오',m:'5'},
  '六':{s:'륙',m:'6'},'七':{s:'칠',m:'7'},'八':{s:'팔',m:'8'},'九':{s:'구',m:'9'},'十':{s:'십',m:'10'},
  '百':{s:'백',m:'100'},'千':{s:'천',m:'1000'},'万':{s:'만',m:'10000'},'两':{s:'량',m:'둘'},
  // 빈출 추가
  '想':{s:'상',m:'생각/원하다'},'养':{s:'양',m:'기르다'},'狗':{s:'구',m:'개'},'猫':{s:'묘',m:'고양이'},
  '加':{s:'가',m:'더하다'},'问':{s:'문',m:'묻다'},'题':{s:'제',m:'문제'},'对':{s:'대',m:'맞다/대해'},
  '错':{s:'착',m:'틀리다'},'真':{s:'진',m:'정말'},'失':{s:'실',m:'잃다'},'望':{s:'망',m:'바라다'},
  '同':{s:'동',m:'같다'},'意':{s:'의',m:'뜻'},'思':{s:'사',m:'생각'},'用':{s:'용',m:'쓰다'},
  '这':{s:'저',m:'이'},'里':{s:'리',m:'안/마을'},'外':{s:'외',m:'바깥'},'边':{s:'변',m:'쪽/가'},
  '左':{s:'좌',m:'왼쪽'},'右':{s:'우',m:'오른쪽'},'里':{s:'리',m:'안'},
  '海':{s:'해',m:'바다'},'山':{s:'산',m:'산'},'河':{s:'하',m:'강'},
  '生':{s:'생',m:'태어나다/날것'},'死':{s:'사',m:'죽다'},'活':{s:'활',m:'살다'},
  '收':{s:'수',m:'받다'},'拾':{s:'십',m:'줍다'},'整':{s:'정',m:'정돈'},'理':{s:'리',m:'다스리다'},
  '搬':{s:'반',m:'옮기다'},'拿':{s:'나',m:'들다'},
  '挂':{s:'괘',m:'걸다'},'摘':{s:'적',m:'따다'},
  '拍':{s:'박',m:'치다/찍다'},'照':{s:'조',m:'비추다/사진'},
  '加':{s:'가',m:'더하다'},'班':{s:'반',m:'근무'},'下':{s:'하',m:'아래'},
  // 추가 빈출 한자
  '可':{s:'가',m:'가능/그러나'},'爱':{s:'애',m:'사랑'},'国':{s:'국',m:'나라'},
  '生':{s:'생',m:'태어나다/날것'},'活':{s:'활',m:'살다'},'本':{s:'본',m:'책/근본'},
  '名':{s:'명',m:'이름'},'字':{s:'자',m:'글자'},'文':{s:'문',m:'글'},'言':{s:'언',m:'말씀'},
  '语':{s:'어',m:'말씀'},'中':{s:'중',m:'가운데'},'日':{s:'일',m:'날/일본'},'韩':{s:'한',m:'한국'},
  '字':{s:'자',m:'글자'},'词':{s:'사',m:'말/단어'},'句':{s:'구',m:'구절'},
  '友':{s:'우',m:'벗'},'朋':{s:'붕',m:'친구'},'同':{s:'동',m:'같다'},'班':{s:'반',m:'반/근무'},
  '父':{s:'부',m:'아버지'},'母':{s:'모',m:'어머니'},'爸':{s:'파',m:'아빠'},'妈':{s:'마',m:'엄마'},
  '哥':{s:'가',m:'형/오빠'},'姐':{s:'저',m:'누나/언니'},'弟':{s:'제',m:'동생(남)'},'妹':{s:'매',m:'여동생'},
  '儿':{s:'아',m:'아이'},'女':{s:'녀',m:'여자'},'男':{s:'남',m:'남자'},'孩':{s:'해',m:'아이'},
  '老':{s:'로',m:'늙다'},'师':{s:'사',m:'스승'},'生':{s:'생',m:'학생/태어나다'},
  '工':{s:'공',m:'장인'},'人':{s:'인',m:'사람'},'位':{s:'위',m:'분(존칭)'},
  '医':{s:'의',m:'의사'},'生':{s:'생',m:'학생'},'警':{s:'경',m:'경찰'},'察':{s:'찰',m:'살피다'},
  '客':{s:'객',m:'손님'},'户':{s:'호',m:'집'},
  // 운동/취미
  '玩':{s:'완',m:'놀다'},'唱':{s:'창',m:'노래'},'歌':{s:'가',m:'노래'},
  '跳':{s:'도',m:'뛰다'},'舞':{s:'무',m:'춤'},'游':{s:'유',m:'헤엄/놀다'},'泳':{s:'영',m:'헤엄'},
  '跑':{s:'포',m:'뛰다'},'步':{s:'보',m:'걸음'},'运':{s:'운',m:'옮기다'},'动':{s:'동',m:'움직이다'},
  '看':{s:'간',m:'보다'},'电':{s:'전',m:'전기'},'影':{s:'영',m:'영화/그림자'},
  '音':{s:'음',m:'소리'},'乐':{s:'락',m:'즐겁다'},'游':{s:'유',m:'놀다'},'戏':{s:'희',m:'놀이'},
  // 자연/날씨
  '太':{s:'태',m:'크다/너무'},'阳':{s:'양',m:'양/해'},'光':{s:'광',m:'빛'},'月':{s:'월',m:'달'},
  '云':{s:'운',m:'구름'},'风':{s:'풍',m:'바람'},'雪':{s:'설',m:'눈'},'冰':{s:'빙',m:'얼음'},
  '春':{s:'춘',m:'봄'},'夏':{s:'하',m:'여름'},'秋':{s:'추',m:'가을'},'冬':{s:'동',m:'겨울'},
  '气':{s:'기',m:'기운/공기'},'温':{s:'온',m:'따뜻'},'度':{s:'도',m:'도수'},
  '花':{s:'화',m:'꽃'},'草':{s:'초',m:'풀'},'树':{s:'수',m:'나무'},'木':{s:'목',m:'나무'},
  // 교통
  '飞':{s:'비',m:'날다'},'机':{s:'기',m:'기계'},'船':{s:'선',m:'배'},'坐':{s:'좌',m:'앉다/타다'},
  '出':{s:'출',m:'나가다'},'租':{s:'조',m:'빌리다'},'巴':{s:'파',m:'(巴士) 버스'},'士':{s:'사',m:'선비'},
  // 신체
  '头':{s:'두',m:'머리'},'发':{s:'발',m:'머리카락/보내다'},'眼':{s:'안',m:'눈'},'睛':{s:'정',m:'눈동자'},
  '鼻':{s:'비',m:'코'},'嘴':{s:'취',m:'입'},'耳':{s:'이',m:'귀'},'朵':{s:'타',m:'송이'},
  '脸':{s:'검',m:'얼굴'},'脖':{s:'박',m:'(脖子) 목'},'肩':{s:'견',m:'어깨'},'背':{s:'배',m:'등'},
  '手':{s:'수',m:'손'},'脚':{s:'각',m:'발'},'腿':{s:'퇴',m:'다리'},'胳':{s:'각',m:'(胳膊) 팔'},'膊':{s:'박',m:'팔'},
  // 색
  '红':{s:'홍',m:'빨강'},'黄':{s:'황',m:'노랑'},'蓝':{s:'람',m:'파랑'},'绿':{s:'록',m:'초록'},
  '黑':{s:'흑',m:'검정'},'白':{s:'백',m:'흰색'},'紫':{s:'자',m:'보라'},'粉':{s:'분',m:'분홍'},
  // 추가 동사
  '出':{s:'출',m:'나가다'},'入':{s:'입',m:'들어가다'},'进':{s:'진',m:'나아가다'},
  '完':{s:'완',m:'끝나다'},'始':{s:'시',m:'시작'},'结':{s:'결',m:'맺다'},'束':{s:'속',m:'묶다'},
  '问':{s:'문',m:'묻다'},'答':{s:'답',m:'답하다'},'解':{s:'해',m:'풀다'},'释':{s:'석',m:'풀다'},
  '同':{s:'동',m:'같다'},'意':{s:'의',m:'뜻'},'反':{s:'반',m:'반대'},'对':{s:'대',m:'맞다'},
  '让':{s:'양',m:'~시키다'},'给':{s:'급',m:'주다'},'帮':{s:'방',m:'돕다'},'忙':{s:'망',m:'바쁘다'},
  '需':{s:'수',m:'(需要) 필요'},'要':{s:'요',m:'요구'},
  '应':{s:'응',m:'(应该) 마땅히'},'该':{s:'해',m:'마땅'},
  '希':{s:'희',m:'바라다'},'望':{s:'망',m:'바라보다'},
  // 양사
  '个':{s:'개',m:'개(양사)'},'本':{s:'본',m:'권'},'张':{s:'장',m:'장'},'件':{s:'건',m:'건'},
  '条':{s:'조',m:'줄/마리'},'只':{s:'척',m:'마리'},'位':{s:'위',m:'분'},
  '杯':{s:'배',m:'잔'},'瓶':{s:'병',m:'병'},'碗':{s:'완',m:'그릇'},
  '次':{s:'차',m:'번'},'遍':{s:'편',m:'번'},'回':{s:'회',m:'번'},
  '种':{s:'종',m:'종류'},'类':{s:'류',m:'종류'},
  // 빈출 추가
  '想要':{s:'상요',m:'원하다'},
  // 캠핑/여행/생활
  '野':{s:'야',m:'들/야외'},'营':{s:'영',m:'경영/숙영'},'帐':{s:'장',m:'천막'},'篷':{s:'봉',m:'(帐篷) 텐트'},
  '搭':{s:'탑',m:'세우다'},'住':{s:'주',m:'살다/머물다'},'宿':{s:'숙',m:'묵다'},
  '旅':{s:'려',m:'여행'},'游':{s:'유',m:'놀다/헤엄'},'行':{s:'행',m:'다니다'},
  '票':{s:'표',m:'표/티켓'},'订':{s:'정',m:'예약'},'消':{s:'소',m:'사라지다'},
  '签':{s:'첨',m:'서명/(签证)'},'证':{s:'증',m:'증명'},
  '护':{s:'호',m:'(护照) 보호'},'照':{s:'조',m:'비추다'},
  // 동작 추가
  '去':{s:'거',m:'가다'},'到':{s:'도',m:'도착/까지'},'达':{s:'달',m:'도달'},
  '回':{s:'회',m:'돌아오다'},'返':{s:'반',m:'돌아오다'},
  '出门':{s:'출문',m:'외출'},
  '进':{s:'진',m:'들어가다'},'入':{s:'입',m:'들어가다'},
  // 음식 추가
  '甜':{s:'첨',m:'달다'},'酸':{s:'산',m:'시다'},'辣':{s:'랄',m:'맵다'},'咸':{s:'함',m:'짜다'},'苦':{s:'고',m:'쓰다'},
  '汤':{s:'탕',m:'국'},'粥':{s:'죽',m:'죽'},'饺':{s:'교',m:'(饺子) 만두'},'子':{s:'자',m:'아이/접미사'},
  '炒':{s:'초',m:'볶다'},'煮':{s:'자',m:'끓이다'},'烤':{s:'고',m:'굽다'},'烧':{s:'소',m:'태우다/굽다'},'蒸':{s:'증',m:'찌다'},
  // 감정 추가
  '怒':{s:'노',m:'화나다'},'惊':{s:'경',m:'놀라다'},'悲':{s:'비',m:'슬프다'},'喜':{s:'희',m:'기쁘다'},
  // 학습/공부
  '语':{s:'어',m:'말'},'文':{s:'문',m:'글'},'课':{s:'과',m:'수업'},'本':{s:'본',m:'책/근본'},
  '题':{s:'제',m:'문제'},'考':{s:'고',m:'시험/생각하다'},'试':{s:'시',m:'시험'},
  // 더 많은 빈출
  '一直':{s:'일직',m:'계속'},'马上':{s:'마상',m:'곧'},
};

// 일본어 사전 (히라가나/카타카나/한자+공통어)
const JP_DICT = {
  // 인사
  'こんにちは':'안녕하세요','ありがとう':'감사합니다','すみません':'죄송합니다',
  'はい':'네','いいえ':'아니요','さようなら':'안녕히 가세요',
  'おはよう':'좋은 아침','こんばんは':'좋은 저녁','おやすみ':'잘 자요',
  'お願いします':'부탁합니다','ごめんなさい':'미안합니다',
  // 대명사
  '私':'저/나','僕':'나(남자)','あなた':'당신','彼':'그','彼女':'그녀',
  'これ':'이것','それ':'그것','あれ':'저것','どれ':'어느 것',
  'ここ':'여기','そこ':'거기','あそこ':'저기','どこ':'어디',
  '何':'무엇','誰':'누구','いつ':'언제','なぜ':'왜',
  // 동사
  '行く':'가다','来る':'오다','見る':'보다','聞く':'듣다',
  '食べる':'먹다','飲む':'마시다','話す':'말하다','読む':'읽다',
  '書く':'쓰다','する':'하다','なる':'되다','ある':'있다','いる':'있다',
  '思う':'생각하다','知る':'알다','分かる':'알다','できる':'할 수 있다',
  '好き':'좋아함','嫌い':'싫어함','欲しい':'원하다',
  // 시간
  '今日':'오늘','明日':'내일','昨日':'어제','今':'지금',
  '朝':'아침','昼':'점심','夜':'밤','夕方':'저녁',
  '時間':'시간','分':'분','秒':'초','年':'년','月':'월','日':'일',
  '今週':'이번 주','来週':'다음 주','先週':'지난 주',
  // 형용사
  '良い':'좋다','悪い':'나쁘다','大きい':'크다','小さい':'작다',
  '高い':'높다/비싸다','安い':'싸다','新しい':'새것','古い':'오래된',
  '楽しい':'재미있다','面白い':'재미있다','つまらない':'재미없다',
  '美味しい':'맛있다','嬉しい':'기쁘다','悲しい':'슬프다',
  // 사물
  '人':'사람','本':'책','車':'차','家':'집','学校':'학교','会社':'회사',
  '電話':'전화','水':'물','お茶':'차','ご飯':'밥','パン':'빵',
  '日本':'일본','韓国':'한국','中国':'중국','英語':'영어',
  // 조사/어미
  'です':'~입니다','ます':'~합니다','でした':'~이었습니다',
  'ですか':'~입니까?','ください':'~해 주세요',
  // 접속사/부사
  'でも':'그러나','そして':'그리고','だから':'그래서','けれども':'그러나',
  'もし':'만약','とても':'매우','少し':'조금','たくさん':'많이',
  // 자주 쓰는 표현
  'おはようございます':'좋은 아침입니다','ありがとうございました':'감사했습니다',
  'いただきます':'잘 먹겠습니다','ごちそうさま':'잘 먹었습니다',
  // 카타카나 자주
  'ホテル':'호텔','レストラン':'레스토랑','コーヒー':'커피','テレビ':'TV',
  'バス':'버스','タクシー':'택시','カメラ':'카메라','コンピューター':'컴퓨터',
};

function isJapanese(text){
  return /[぀-ゟ゠-ヿ]/.test(text);  // 히라가나/가타카나
}
function isChinese(text){
  // 한자가 있고 가나가 없으면 중국어로 간주
  return /[一-鿿]/.test(text) && !isJapanese(text);
}
function isVietnamese(text){
  // 베트남어 특수 발음표기 (đ, ơ, ư, ã, ạ, ấ 등)
  return /[ăâđêôơưĂÂĐÊÔƠƯàáạảãằắặẳẵầấậẩẫèéẹẻẽềếệểễìíịỉĩòóọỏõồốộổỗờớợởỡùúụủũừứựửữỳýỵỷỹÀ-Ỹ]/.test(text);
}
function isEnglish(text){
  // 라틴 알파벳만 (한자/가나/베트남 발음표기 없음)
  return /[A-Za-z]/.test(text) && !/[一-鿿぀-ヿ]/.test(text) && !isVietnamese(text);
}
function detectLang(text){
  if(isJapanese(text)) return 'ja';
  if(isVietnamese(text)) return 'vi';
  if(isChinese(text)) return 'zh';
  if(isEnglish(text)) return 'en';
  return 'zh';  // 기본값: 한자 위주
}
const LANG_LABEL = {ja:'일본어', zh:'중국어', vi:'베트남어', en:'영어'};

function analyzeJapanese(text){
  const words = [];
  let i = 0;
  while(i < text.length){
    let matched = false;
    for(let len = 8; len >= 2; len--){
      if(i + len <= text.length){
        const sub = text.substring(i, i+len);
        if(JP_DICT[sub]){
          words.push({type:'word', text:sub, meaning:JP_DICT[sub]});
          i += len;
          matched = true;
          break;
        }
      }
    }
    if(!matched){
      const ch = text[i];
      // single hiragana/katakana - skip
      // single kanji - try hanja dict
      if(/[一-鿿]/.test(ch)){
        const e = HANJA_DICT[ch];
        words.push({type:'char', text:ch, sound:e?e.s:'', meaning:e?e.m:''});
      } else if(/[ぁ-んァ-ヶ]/.test(ch)){
        // accumulate kana
        let kana = ch;
        let j = i + 1;
        while(j < text.length && /[ぁ-んァ-ヶー]/.test(text[j])){
          kana += text[j]; j++;
        }
        words.push({type:'kana', text:kana, meaning:'(가나)'});
        i = j; continue;
      }
      i++;
    }
  }
  return words;
}
const WORD_DICT = {
  '你好':'안녕하세요','谢谢':'감사합니다','对不起':'미안합니다','没关系':'괜찮습니다',
  '请问':'실례합니다','再见':'안녕히 가세요','早上好':'좋은 아침',
  '今天':'오늘','明天':'내일','昨天':'어제','现在':'지금','以前':'예전','以后':'이후',
  '什么':'무엇','谁':'누구','哪里':'어디','为什么':'왜','怎么':'어떻게','怎么样':'어때',
  '我们':'우리','你们':'너희','他们':'그들',
  '喜欢':'좋아하다','讨厌':'싫어하다','想要':'원하다','需要':'필요하다',
  '可以':'~해도 된다','可能':'아마도','应该':'~해야 한다','一定':'반드시',
  '吃饭':'밥 먹다','喝水':'물 마시다','看电视':'TV 보다','听音乐':'음악 듣다',
  '说话':'말하다','睡觉':'잠자다','起床':'일어나다','洗澡':'샤워하다',
  '工作':'일하다','上班':'출근','下班':'퇴근','加班':'야근','休息':'쉬다',
  '回家':'집에 가다','出去':'나가다','进来':'들어오다','上来':'올라오다',
  '电话':'전화','手机':'휴대폰','电脑':'컴퓨터','电视':'TV','电梯':'엘리베이터',
  '咖啡':'커피','面包':'빵','米饭':'밥','水果':'과일','蔬菜':'채소',
  '北京':'베이징','上海':'상하이','中国':'중국','韩国':'한국',
  '高兴':'기쁘다','开心':'즐겁다','难过':'슬프다','生气':'화나다','害怕':'무섭다',
  '舒服':'편안하다','麻烦':'귀찮다','困难':'어렵다','简单':'간단하다',
  '便宜':'싸다','贵':'비싸다','漂亮':'예쁘다','可爱':'귀엽다','聪明':'똑똑하다',
  '感冒':'감기','头疼':'두통','发烧':'열나다','咳嗽':'기침',
  '一直':'계속','马上':'곧','一会儿':'잠시 후','差不多':'거의','一定':'반드시',
  '其实':'사실','当然':'당연히','总是':'항상','经常':'자주','偶尔':'가끔',
  '所以':'그래서','但是':'그러나','因为':'~때문에','如果':'만약','虽然':'비록',
  '帮我':'저를 도와','一下':'잠깐','一点儿':'조금','点儿':'조금',
  '野营':'야영/캠핑','旅游':'여행','旅行':'여행','回来':'돌아오다','回去':'돌아가다',
  '出去':'나가다','进来':'들어오다','上来':'올라오다','下来':'내려오다',
  '帐篷':'텐트','睡袋':'침낭','背包':'배낭',
  '高兴':'기쁘다','开心':'즐겁다','难过':'슬프다','生气':'화나다','害怕':'무섭다',
  '好吃':'맛있다','好喝':'맛있다(음료)','好看':'보기 좋다','好听':'듣기 좋다',
  '不错':'괜찮다/좋다','差不多':'거의','马马虎虎':'그저 그렇다',
  '一起':'함께','一会儿':'잠시',
  '怎么办':'어떻게 하지','怎么了':'왜 그래','没事':'괜찮다','没问题':'문제없다',
  '加油':'힘내!','请问':'실례합니다','麻烦你了':'수고하셨어요','拜拜':'바이바이',
  '左边':'왼쪽','右边':'오른쪽','前边':'앞쪽','后边':'뒤쪽','里边':'안쪽','外边':'바깥쪽',
  '上边':'위쪽','下边':'아래쪽','旁边':'옆','中间':'가운데','附近':'근처',
  '春天':'봄','夏天':'여름','秋天':'가을','冬天':'겨울',
  '早上':'아침','上午':'오전','中午':'점심','下午':'오후','晚上':'저녁',
  '正在':'~하는 중','已经':'이미','刚才':'방금','马上':'곧',
};

// === OpenRouter API key (선택) ===
async function checkApiKey(){
  try {
    const r = await fetch('/api/key');
    const d = await r.json();
    const status = document.getElementById('ai-status');
    const btn = document.getElementById('ai-btn');
    if(d.set){
      if(status) status.innerHTML = `<span style="color:#22c55e">✅ 연결됨</span> <span style="color:#6b7280">(${d.preview})</span>`;
      if(btn){ btn.style.display = ''; btn.disabled = false; }
    } else {
      if(status) status.innerHTML = '<span style="color:#fbbf24">미연결</span> — 키 입력 시 AI 기능 활성';
      if(btn) btn.style.display = 'none';
    }
  } catch(e){}
}
async function setApiKey(){
  const cur = await fetch('/api/key').then(r=>r.json());
  const msg = cur.set ? `현재: ${cur.preview}\n새 키 입력 (취소: ESC)` : 'OpenRouter API 키 입력 (sk-or-v1-...)\n발급: https://openrouter.ai/keys';
  const k = prompt(msg, '');
  if(!k) return;
  const r = await fetch('/api/key', {method:'POST', body: new URLSearchParams({key: k.trim()})});
  const d = await r.json();
  if(d.error){ alert('오류: '+d.error); return; }
  alert('✅ API 키 저장됨');
  checkApiKey();
}
checkApiKey();

function lookupExternal(){
  const text = document.getElementById('explain-input').value.trim();
  if(!text){ alert('텍스트를 입력하세요 (자막 캡처 또는 직접 입력)'); return; }
  const lang = detectLang(text);
  const q = encodeURIComponent(text);
  const urlMap = {
    ja: `https://ja.dict.naver.com/#/search?query=${q}`,
    zh: `https://zh.dict.naver.com/#/search?query=${q}`,
    vi: `https://dict.naver.com/vikodict/#/search?query=${q}`,
    en: `https://en.dict.naver.com/#/search?query=${q}`,
  };
  window.open(urlMap[lang] || urlMap.zh, '_blank', 'noopener');
}

async function aiAnalyzeText(){
  const text = document.getElementById('explain-input').value.trim();
  if(!text){ alert('텍스트를 입력하세요 (자막 캡처 또는 직접 입력)'); return; }
  const lang = detectLang(text);
  const status = document.getElementById('explain-out');
  status.classList.add('show');
  status.innerHTML = `<div style="text-align:center;padding:20px;color:#fbbf24">AI 분석 중... (${LANG_LABEL[lang]} 감지, ~3-8초)</div>`;
  try {
    const r = await fetch('/api/ai_text', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, lang})
    });
    const data = await r.json();
    if(data.error){
      if(data.error.includes('API 키')){
        status.innerHTML = '<div style="color:#ef4444">OpenRouter API 키 미설정 — [설정] 메뉴에서 키 입력</div>';
      } else {
        status.innerHTML = '<div style="color:#ef4444">오류: '+data.error+'</div>';
      }
      return;
    }
    status.innerHTML = `<div style="white-space:pre-wrap;line-height:1.7;font-size:14px;color:#e5e7eb">${data.text.replace(/</g,'&lt;')}</div>`;
    markHasResult();
  } catch(e){
    status.innerHTML = '<div style="color:#ef4444">AI 오류: '+e.message+'</div>';
  }
}

async function aiAnalyze(){
  let blob;
  if(curMeta && mode === 'download'){
    // Use captured frame from video
    pause();
    const t = getCurrentTime();
    const r = await fetch('/api/capture', {method:'POST', body: new URLSearchParams({pid: curMeta.id, time: t})});
    const data = await r.json();
    if(data.error){
      alert('영상 캡처 실패. 이미지 직접 업로드(🖼) 후 AI 분석.'); return;
    }
    // We have text from server-side capture via OCR. But for AI, we need the IMAGE. Fall back to client capture if possible.
  }
  // Easier path: use the file picker for AI
  if(!confirm('AI 분석은 이미지 파일이 필요합니다. 파일 선택 또는 Ctrl+V로 이미지 붙여넣기 하시겠어요?\n\n예: 파일 선택 / 아니오: 취소')){
    return;
  }
  document.getElementById('img-input').dataset.useAi = '1';
  document.getElementById('img-input').click();
}

async function ocrViaAI(blob){
  const status = document.getElementById('explain-out');
  status.classList.add('show');
  status.innerHTML = '<div style="text-align:center;padding:20px;color:#fbbf24">AI 분석 중... (~5-10초)</div>';
  try {
    const r = await fetch('/api/ai', {method:'POST', body: blob, headers:{'Content-Type': blob.type || 'image/jpeg'}});
    const data = await r.json();
    if(data.error){ status.innerHTML = '<div style="color:#ef4444">오류: '+data.error+'</div>'; return; }
    // Show AI response directly
    status.innerHTML = `<div style="white-space:pre-wrap;line-height:1.6;font-size:14px">${data.text.replace(/</g,'&lt;')}</div>`;
    // also extract first non-empty line as 원문 for textarea
    const firstLine = data.text.split('\n').find(l => l.trim() && !l.includes('번역:') && !l.includes('주요'));
    if(firstLine){
      document.getElementById('explain-input').value = firstLine.replace(/^원문:\s*/, '').trim();
    }
  } catch(e){
    status.innerHTML = '<div style="color:#ef4444">AI 오류: '+e.message+'</div>';
  }
}

async function handleImageFile(file){
  if(!file) return;
  if(!file.type.startsWith('image/')){ alert('이미지 파일만 가능'); return; }
  const useAi = document.getElementById('img-input').dataset.useAi === '1';
  document.getElementById('img-input').dataset.useAi = '';
  if(useAi){ await ocrViaAI(file); }
  else { await ocrImageBlob(file); }
}

async function ocrImageBlob(blob){
  const status = document.getElementById('explain-out');
  status.classList.add('show');
  status.innerHTML = '<div style="text-align:center;padding:20px;color:#fbbf24">⏳ OCR 중... (~2초)</div>';
  try {
    const r = await fetch('/api/ocr', {method:'POST', body: blob, headers:{'Content-Type': blob.type || 'image/jpeg'}});
    const data = await r.json();
    if(data.error){ status.innerHTML = '<div style="color:#ef4444">오류: '+data.error+'</div>'; return; }
    if(!data.text){ status.innerHTML = '<div style="color:#9ca3af;text-align:center;padding:20px">텍스트 인식 실패</div>'; return; }
    document.getElementById('explain-input').value = data.text;
    analyzeChinese();
  } catch(e){
    status.innerHTML = '<div style="color:#ef4444">오류: '+e.message+'</div>';
  }
}

// Listen for clipboard paste of image (Ctrl+V on PC)
document.addEventListener('paste', e => {
  if(!document.getElementById('explain') || document.getElementById('explain').style.display === 'none') return;
  for(const item of e.clipboardData.items){
    if(item.type.startsWith('image/')){
      const blob = item.getAsFile();
      if(blob){ e.preventDefault(); ocrImageBlob(blob); break; }
    }
  }
});

async function captureAndAnalyze(){
  if(!curMeta){ alert('영상 선택 먼저'); return; }
  const status = document.getElementById('explain-out');
  status.classList.add('show');
  // Pause whichever mode
  pause();
  const t = getCurrentTime();
  status.innerHTML = `<div style="text-align:center;padding:20px;color:#fbbf24">⏳ ${fmt(t)} 시점 캡처 + OCR 중... (~2-5초)</div>`;
  try {
    const body = new URLSearchParams({pid: curMeta.id, time: t});
    const r = await fetch('/api/capture', {method:'POST', body});
    const data = await r.json();
    if(data.error){ status.innerHTML = '<div style="color:#ef4444">오류: '+data.error+'</div>'; return; }
    if(!data.text){ status.innerHTML = '<div style="color:#9ca3af;text-align:center;padding:20px">텍스트 인식 실패 (다른 시점에서 시도)</div>'; return; }
    document.getElementById('explain-input').value = data.text;
    analyzeChinese();
  } catch(e){
    status.innerHTML = '<div style="color:#ef4444">캡처 오류: '+e.message+'</div>';
  }
}

function analyzeChinese(){
  const text = document.getElementById('explain-input').value.trim();
  if(!text){ alert('텍스트를 입력하세요'); return; }
  const out = document.getElementById('explain-out');
  const lang = detectLang(text);
  out.innerHTML = '';
  out.classList.add('show');

  // 베트남어/영어는 로컬 사전 없음 → 안내
  if(lang === 'vi' || lang === 'en'){
    out.innerHTML = `<div style="text-align:center;padding:14px;color:#9ca3af">
      <div style="color:#fbbf24;font-size:15px;margin-bottom:8px">${LANG_LABEL[lang]} 감지</div>
      <div style="font-size:13px">로컬 사전 미지원 — <b>네이버</b> 버튼으로 외부 사전 사용</div>
    </div>`;
    markHasResult();
    return;
  }

  let words;
  if(lang === 'ja'){
    words = analyzeJapanese(text);
  } else {
    // Chinese analysis
    words = [];
    let i = 0;
    while(i < text.length){
      let matched = false;
      for(let len = 4; len >= 2; len--){
        if(i + len <= text.length){
          const sub = text.substring(i, i+len);
          if(WORD_DICT[sub]){
            words.push({type:'word', text:sub, meaning:WORD_DICT[sub]});
            i += len; matched = true; break;
          }
        }
      }
      if(!matched){
        const ch = text[i];
        if(/[一-鿿]/.test(ch)){
          const e = HANJA_DICT[ch];
          words.push({type:'char', text:ch, sound:e?e.s:'', meaning:e?e.m:''});
        }
        i++;
      }
    }
  }

  let html = '';
  for(const w of words){
    if(w.type === 'word'){
      html += `<div class="row-w word-row"><span class="ch">${w.text}</span><span class="meaning">${w.meaning}</span></div>`;
    } else if(w.type === 'kana'){
      html += `<div class="row-w"><span class="ch">${w.text}</span><span class="meaning">${w.meaning}</span></div>`;
    } else {
      if(w.meaning){
        html += `<div class="row-w"><span class="ch">${w.text}</span><span class="ko-sound">${w.sound}</span><span class="meaning">${w.meaning}</span></div>`;
      } else {
        html += `<div class="row-w"><span class="ch">${w.text}</span><span class="unknown">사전에 없음</span></div>`;
      }
    }
  }
  if(!html) html = '<div class="unknown">한자/일본어가 발견되지 않았습니다</div>';
  out.innerHTML = html;
  markHasResult();
}

async function pasteUrl(){
  try {
    const txt = await navigator.clipboard.readText();
    document.getElementById('m-url').value = txt.trim();
    const m = txt.match(/(?:v=|youtu\.be\/|\/shorts\/)([A-Za-z0-9_-]{11})/);
    if(m && !document.getElementById('m-pid').value){
      document.getElementById('m-pid').value = 'v_' + m[1].slice(0, 8);
    }
  } catch(e){ alert('붙여넣기 권한 거부됨. 직접 붙여넣어 주세요.'); }
}
let polling = null;
async function startProcess(){
  const url = document.getElementById('m-url').value.trim();
  const pid = document.getElementById('m-pid').value.trim();
  if(!url || !pid){ alert('URL과 ID 모두 입력하세요'); return; }
  if(!/^[a-zA-Z0-9_-]+$/.test(pid)){ alert('ID는 영문/숫자/_/-만'); return; }
  const data = new URLSearchParams({pid, url, mode: document.getElementById('m-mode').value,
    noise: document.getElementById('m-noise').value, silence: document.getElementById('m-silence').value});
  document.getElementById('m-status').textContent = '시작 중...';
  document.getElementById('m-log').style.display = 'block';
  document.getElementById('m-log').textContent = '';
  try {
    const r = await fetch('/api/process', {method:'POST', body: data});
    const resp = await r.json();
    if(resp.error){ document.getElementById('m-status').textContent = '오류: '+resp.error; return; }
    document.getElementById('m-status').textContent = '⏳ 처리 중 (10-15분)...';
    polling = setInterval(() => pollStatus(pid), 1500);
  } catch(e){ document.getElementById('m-status').textContent = '오류: '+e.message; }
}
async function pollStatus(pid){
  try {
    const r = await fetch('/api/status?pid='+encodeURIComponent(pid));
    const data = await r.json();
    document.getElementById('m-log').textContent = (data.log||[]).join('\n');
    document.getElementById('m-log').scrollTop = 99999;
    if(data.status === 'done'){
      clearInterval(polling);
      document.getElementById('m-status').textContent = '✅ 완료!';
      loadProjects();
      setTimeout(() => {
        document.getElementById('vid-select').value = pid;
        document.getElementById('vid-select').dispatchEvent(new Event('change'));
        closeModal();
      }, 1500);
    } else if(data.status === 'failed'){
      clearInterval(polling);
      document.getElementById('m-status').textContent = '❌ 실패';
    }
  } catch(e){}
}

document.addEventListener('keydown', e => {
  if(e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if(e.key === 'ArrowLeft'){ prevSeg(); e.preventDefault(); }
  else if(e.key === 'ArrowRight'){ nextSeg(); e.preventDefault(); }
  else if(e.key === ' '){ togglePlay(); e.preventDefault(); }
});

// === Drag & Drop file upload ===
const overlay = document.getElementById('drop-overlay');
let dragCounter = 0;
window.addEventListener('dragenter', e => { if(e.dataTransfer.types.includes('Files')){ e.preventDefault(); dragCounter++; overlay.classList.add('show'); }});
window.addEventListener('dragleave', e => { e.preventDefault(); dragCounter--; if(dragCounter <= 0){ overlay.classList.remove('show'); dragCounter = 0; }});
window.addEventListener('dragover', e => { if(e.dataTransfer.types.includes('Files')) e.preventDefault(); });
window.addEventListener('drop', e => {
  e.preventDefault();
  overlay.classList.remove('show'); dragCounter = 0;
  const file = e.dataTransfer.files[0];
  if(file) handleFile(file);
});
document.getElementById('file-input').addEventListener('change', e => {
  if(e.target.files[0]) handleFile(e.target.files[0]);
});
async function handleFile(file){
  if(!file.type.startsWith('video/') && !/\.(mp4|mkv|webm|mov|avi)$/i.test(file.name)){
    alert('영상 파일만 가능합니다 (mp4/mkv/webm/mov)'); return;
  }
  const sizeMB = (file.size / 1048576).toFixed(1);
  const defaultPid = 'local_' + Date.now().toString(36);
  const pid = prompt(`프로젝트 ID 입력 (영문/숫자/_):\n파일: ${file.name} (${sizeMB}MB)`, defaultPid);
  if(!pid) return;
  if(!/^[a-zA-Z0-9_-]+$/.test(pid)){ alert('ID는 영문/숫자/_/-만'); return; }
  const dispName = prompt('표시 이름 (한글 가능):', file.name.replace(/\.[^.]+$/, ''));
  if(!dispName) return;
  const fd = new FormData();
  fd.append('pid', pid); fd.append('name', dispName);
  fd.append('mode', 'download'); fd.append('noise', '-30'); fd.append('silence', '0.4');
  fd.append('file', file);
  setDropMsg(`📤 업로드 중... 0%`);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = e => {
    if(e.lengthComputable){
      const pct = Math.round(e.loaded / e.total * 100);
      setDropMsg(`📤 업로드 중... ${pct}% (${(e.loaded/1048576).toFixed(0)}/${(e.total/1048576).toFixed(0)}MB)`);
    }
  };
  xhr.onload = () => {
    try {
      const data = JSON.parse(xhr.responseText);
      if(data.error){ alert('오류: '+data.error); resetDropZone(); return; }
      fetch('/api/rename', {method:'POST', body: new URLSearchParams({pid, name: dispName})});
      setDropMsg('✅ 업로드 완료. 무음 감지 + 프레임 처리 중... (몇 분 소요)');
      const poll = setInterval(async () => {
        const r = await fetch('/api/status?pid='+encodeURIComponent(pid));
        const s = await r.json();
        if(s.status === 'done'){
          clearInterval(poll);
          resetDropZone();
          loadProjects();
          if(confirm('처리 완료. 바로 재생할까요?')) openProject(pid);
        } else if(s.status === 'failed'){
          clearInterval(poll);
          setDropMsg('❌ 처리 실패. 콘솔에서 로그 확인.');
        }
      }, 2000);
    } catch(e){ alert(e.message); resetDropZone(); }
  };
  xhr.onerror = () => { alert('업로드 실패'); resetDropZone(); };
  xhr.send(fd);
}
function setDropMsg(msg){ document.getElementById('drop-zone').innerHTML = `<div style="padding:14px;font-size:15px">${msg}</div>`; }
function resetDropZone(){
  document.getElementById('drop-zone').innerHTML = `<div class="big">파일</div>
    <div><b>로컬 영상 파일 드래그</b> 하거나 <b>클릭하여 선택</b></div>
    <div class="hint">mp4, mkv, webm, mov 등 / 자동으로 처리 시작</div>`;
}

loadProjects();
</script>
</body>
</html>
'''

def _serve_with_range(handler, file_path, content_type='application/octet-stream'):
    """Serve a file with HTTP Range request support (required for video seeking)."""
    file_size = os.path.getsize(file_path)
    range_header = handler.headers.get('Range')
    if range_header:
        m = re.match(r'bytes=(\d*)-(\d*)', range_header)
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1
            handler.send_response(206)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            handler.send_header('Content-Length', str(length))
            handler.send_header('Cache-Control', 'no-cache')
            handler.end_headers()
            with open(file_path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk: break
                    try: handler.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError): break
                    remaining -= len(chunk)
            return
    # No Range header: full file
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(file_size))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk: break
            try: handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError): break

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache')
        super().end_headers()

    def _check_auth(self):
        # RP_USER+RP_PASS 환경변수 설정 시 BasicAuth 요구
        if not RP_USER or not RP_PASS:
            return True
        import base64
        h = self.headers.get('Authorization', '')
        if h.startswith('Basic '):
            try:
                decoded = base64.b64decode(h[6:]).decode('utf-8')
                user, pwd = decoded.split(':', 1)
                if user == RP_USER and pwd == RP_PASS:
                    return True
            except Exception:
                pass
        # 인증 실패 → 401
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Repeat Player"')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write('인증이 필요합니다 (가족 공유용)'.encode('utf-8'))
        return False

    def do_GET(self):
        if not self._check_auth(): return
        if self.path in ('/', '/index.html'):
            body = INDEX_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/api/list':
            return self._json(list_projects())
        if self.path.startswith('/api/meta'):
            qs = parse_qs(urlparse(self.path).query)
            pid = qs.get('pid', [''])[0]
            meta = get_meta(pid)
            return self._json(meta or {'error':'not found'}, 200 if meta else 404)
        if self.path.startswith('/api/status'):
            qs = parse_qs(urlparse(self.path).query)
            pid = qs.get('pid', [''])[0]
            with JOB_LOCK:
                return self._json(dict(JOBS.get(pid, {'status':'unknown','log':[]})))
        if self.path == '/api/key':
            key = get_or_key()
            return self._json({'set': bool(key), 'preview': (key[:10]+'...' if key else '')})
        if self.path == '/api/lanip':
            self.send_header_cors = True
            import socket
            ip = ''
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass
            return self._json({'ip': ip})
        # Video/audio files: support Range requests for seeking
        path = self.path.lstrip('/')
        from urllib.parse import unquote
        path = unquote(path.split('?')[0])
        full_path = os.path.join(str(OUT_DIR), path)
        if os.path.isfile(full_path):
            ext = path.lower().rsplit('.', 1)[-1] if '.' in path else ''
            if ext in ('mp4','m4a','webm','mkv','mov','aac','mp3','ogg'):
                ctype = {'mp4':'video/mp4','webm':'video/webm','mkv':'video/x-matroska',
                         'mov':'video/quicktime','m4a':'audio/mp4','aac':'audio/aac',
                         'mp3':'audio/mpeg','ogg':'audio/ogg'}.get(ext, 'application/octet-stream')
                _serve_with_range(self, full_path, ctype)
                return
        return super().do_GET()

    def do_POST(self):
        if not self._check_auth(): return
        if self.path == '/api/ai':
            length = int(self.headers.get('Content-Length', 0))
            img_bytes = self.rfile.read(length)
            prompt = self.headers.get('X-Prompt') or (
                "이 이미지에 보이는 외국어 텍스트(중국어/일본어/영어/베트남어)를 정확히 추출하고 한국어로 해석해주세요. "
                "형식:\n원문: <원본 텍스트>\n번역: <한국어 번역>\n주요 단어:\n- 단어1: 뜻\n- 단어2: 뜻"
            )
            result = call_openrouter_vision(img_bytes, prompt)
            return self._json(result, 200 if result.get('ok') else 500)
        if self.path == '/api/ai_text':
            length = int(self.headers.get('Content-Length', 0))
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
            except Exception:
                return self._json({'error': 'invalid JSON'}, 400)
            text = (payload.get('text') or '').strip()
            lang = (payload.get('lang') or 'zh').strip()
            if not text:
                return self._json({'error': '텍스트 없음'}, 400)
            PROMPTS = {
                'zh': (
                    f"다음 중국어 간자체 문장을 한국어 학습자에게 설명해주세요:\n「{text}」\n\n"
                    "형식:\n번역: <한국어 번역>\n발음(병음): <pinyin>\n글자별 풀이:\n"
                    "- 글자1 [병음]: 한국어 뜻\n주요 단어/표현:\n- 단어1: 뜻"
                ),
                'ja': (
                    f"다음 일본어 문장을 한국어 학습자에게 설명해주세요:\n「{text}」\n\n"
                    "형식:\n번역: <한국어 번역>\n발음(히라가나/요미가나): <kana>\n로마자: <romaji>\n"
                    "단어 분석:\n- 단어1 [요미가나]: 한국어 뜻\n주요 문법:\n- 문법1: 설명"
                ),
                'vi': (
                    f"다음 베트남어 문장을 한국어 학습자에게 설명해주세요:\n「{text}」\n\n"
                    "형식:\n번역: <한국어 번역>\n발음 가이드(IPA 또는 한국어 표기): <발음>\n"
                    "단어 분석:\n- 단어1: 한국어 뜻\n문법/표현:\n- 표현1: 설명"
                ),
                'en': (
                    f"다음 영어 문장을 한국어 학습자에게 설명해주세요:\n「{text}」\n\n"
                    "형식:\n번역: <한국어 번역>\n발음(IPA): <ipa>\n핵심 단어:\n"
                    "- word1 [발음]: 한국어 뜻\n문법 포인트:\n- 포인트1: 설명"
                ),
            }
            prompt = PROMPTS.get(lang, PROMPTS['zh'])
            result = call_openrouter_text(prompt)
            if result.get('ok'):
                result['lang'] = lang
            return self._json(result, 200 if result.get('ok') else 500)
        if self.path == '/api/key':
            length = int(self.headers.get('Content-Length', 0))
            params = parse_qs(self.rfile.read(length).decode('utf-8'))
            key = params.get('key', [''])[0].strip()
            if not key.startswith('sk-'):
                return self._json({'error': 'sk-로 시작하는 OpenRouter 키 입력'}, 400)
            set_or_key(key)
            return self._json({'ok': True})
        if self.path == '/api/ocr':
            length = int(self.headers.get('Content-Length', 0))
            img_bytes = self.rfile.read(length)
            try:
                text = ocr_image_bytes(img_bytes)
                return self._json({'ok': True, 'text': text})
            except Exception as e:
                return self._json({'error': str(e)}, 500)
        if self.path == '/api/capture':
            length = int(self.headers.get('Content-Length', 0))
            params = parse_qs(self.rfile.read(length).decode('utf-8'))
            pid = params.get('pid', [''])[0]
            t = params.get('time', ['0'])[0]
            meta = get_meta(pid)
            if not meta:
                return self._json({'error': 'project not found'}, 404)
            video = OUT_DIR / meta.get('video_file', '')
            if not video.exists():
                return self._json({'error': 'video file missing'}, 404)
            # Extract frame at time t
            import imageio_ffmpeg, tempfile
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            tmp.close()
            try:
                cmd = [ffmpeg, '-y', '-ss', str(float(t)), '-i', str(video),
                       '-frames:v', '1', '-q:v', '3', '-an',
                       tmp.name, '-loglevel', 'error']
                subprocess.run(cmd, capture_output=True, timeout=45)
                if not os.path.exists(tmp.name) or os.path.getsize(tmp.name) == 0:
                    return self._json({'error': 'frame extract failed'}, 500)
                with open(tmp.name, 'rb') as f:
                    img = f.read()
                # Crop middle band before OCR (where Chinese text usually is)
                import cv2, numpy as np
                arr = np.frombuffer(img, dtype=np.uint8)
                full = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if full is not None:
                    H = full.shape[0]
                    crop = full[int(H*0.30):int(H*0.70), :]
                    _, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    img = buf.tobytes()
                # Try local OCR first
                ocr_err = None
                text = ''
                try:
                    text = ocr_image_bytes(img)
                except Exception as e:
                    ocr_err = str(e)
                # Fall back to AI vision if OCR failed or returned empty (need API key)
                if not text and get_or_key():
                    prompt = (
                        "이미지에 보이는 외국어 자막(중국어/일본어/영어)만 정확히 한 줄로 추출하세요. "
                        "다른 설명·번역은 하지 말고 원문 텍스트만 응답하세요. 자막이 없으면 빈 문자열로."
                    )
                    ai = call_openrouter_vision(img, prompt)
                    if ai.get('ok'):
                        text = (ai.get('text') or '').strip().splitlines()[0] if ai.get('text') else ''
                        return self._json({'ok': True, 'text': text, 'time': float(t), 'source': 'ai'})
                    elif ocr_err:
                        return self._json({'error': f'OCR/AI 모두 실패: {ocr_err}; AI: {ai.get("error","")}'}, 500)
                if ocr_err and not text:
                    return self._json({'error': f'OCR 실패 (AI 키 미설정): {ocr_err}'}, 500)
                return self._json({'ok': True, 'text': text, 'time': float(t), 'source': 'ocr'})
            except Exception as e:
                return self._json({'error': str(e)}, 500)
            finally:
                try: os.unlink(tmp.name)
                except: pass
        if self.path == '/api/upload':
            ctype = self.headers.get('Content-Type', '')
            m = re.search(r'boundary=([^;]+)', ctype)
            if not m:
                return self._json({'error': 'no boundary'}, 400)
            boundary = m.group(1).strip().strip('"')
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                fields = parse_multipart(body, boundary)
            except Exception as e:
                return self._json({'error': f'parse failed: {e}'}, 400)
            pid = fields.get('pid', {}).get('value', '').strip()
            name = fields.get('name', {}).get('value', '').strip() or pid
            file = fields.get('file')
            if not pid or not file or 'data' not in file:
                return self._json({'error': 'pid and file required'}, 400)
            if not re.match(r'^[a-zA-Z0-9_-]+$', pid):
                return self._json({'error': 'invalid pid'}, 400)
            target = OUT_DIR / f'{pid}.mp4'
            target.write_bytes(file['data'])
            # Trigger processing in background
            mode = fields.get('mode', {}).get('value', 'download')
            noise = int(fields.get('noise', {}).get('value', '-30'))
            silence = float(fields.get('silence', {}).get('value', '0.4'))
            t = threading.Thread(target=process_video_async,
                                 args=(pid, str(target), mode, noise, silence), daemon=True)
            t.start()
            # Save name if provided
            return self._json({'ok': True, 'pid': pid, 'size_mb': round(len(file['data'])/1048576, 1)})
        if self.path == '/api/rename':
            length = int(self.headers.get('Content-Length', 0))
            params = parse_qs(self.rfile.read(length).decode('utf-8'))
            pid = params.get('pid', [''])[0]
            new_name = params.get('name', [''])[0].strip()
            if not pid or not new_name:
                return self._json({'error': 'pid and name required'}, 400)
            meta_file = OUT_DIR / f'{pid}.meta.json'
            if not meta_file.exists():
                return self._json({'error': 'project not found'}, 404)
            try:
                meta = json.loads(meta_file.read_text(encoding='utf-8'))
                meta['name'] = new_name
                meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
                return self._json({'ok': True, 'name': new_name})
            except Exception as e:
                return self._json({'error': str(e)}, 500)
        if self.path == '/api/delete':
            length = int(self.headers.get('Content-Length', 0))
            params = parse_qs(self.rfile.read(length).decode('utf-8'))
            pid = params.get('pid', [''])[0]
            if not pid or not re.match(r'^[a-zA-Z0-9_-]+$', pid):
                return self._json({'error': 'invalid pid'}, 400)
            removed = []
            for ext in ['.meta.json', '.segments.json', '.html', '.mp4', '.m4a']:
                f = OUT_DIR / f'{pid}{ext}'
                if f.exists():
                    try: f.unlink(); removed.append(f.name)
                    except: pass
            return self._json({'ok': True, 'removed': removed})
        if self.path == '/api/process':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            params = parse_qs(body)
            pid = params.get('pid', [''])[0]
            url = params.get('url', [''])[0]
            mode = params.get('mode', ['both'])[0]
            noise = params.get('noise', ['-30'])[0]
            silence = params.get('silence', ['0.4'])[0]
            if not pid or not url:
                return self._json({'error': 'pid/url required'}, 400)
            if not re.match(r'^[a-zA-Z0-9_-]+$', pid):
                return self._json({'error': 'invalid pid'}, 400)
            with JOB_LOCK:
                if pid in JOBS and JOBS[pid].get('status') == 'running':
                    return self._json({'error': 'already running'}, 409)
            t = threading.Thread(target=process_video_async,
                                 args=(pid, url, mode, int(noise), float(silence)), daemon=True)
            t.start()
            return self._json({'ok': True, 'pid': pid})
        self.send_error(404)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args): pass

    def handle_one_request(self):
        # 클라이언트가 끊은 connection 트레이스백을 조용히 삼킴
        try:
            return super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except Exception:
            # 기타 예외는 기본 동작 (이미 응답이 쓰였을 수 있음)
            self.close_connection = True

    def finish(self):
        try: super().finish()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

def detect_lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''

def main():
    os.chdir(str(OUT_DIR))
    lan = detect_lan_ip()
    print(f'\n=== 반복 학습 플레이어 서버 ===')
    print(f'PC : http://localhost:{PORT}/')
    if lan:
        print(f'폰 : http://{lan}:{PORT}/   (같은 Wi-Fi)')
    print(f'Files: {OUT_DIR}')
    # OpenRouter 키 안내
    if not get_or_key():
        print(f'\n참고: OpenRouter 키 미설정 → AI 해석 비활성. ⚙ 설정 메뉴에서 입력하세요.')
    print('종료: Ctrl+C\n')
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try: srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[서버 종료]')
        srv.shutdown()

if __name__ == '__main__':
    main()
