"""
세그먼트별 한자/일어 텍스트를 OCR로 추출 → segments JSON에 추가.

사용법:
  python enrich_segments.py <project_id>      # 단일 프로젝트
  python enrich_segments.py --all              # 모든 프로젝트
  python enrich_segments.py --all --workers 4  # 4-worker 병렬

수행:
  1. 영상 mp4 파일에서 각 세그먼트의 중간 시점 프레임 추출
  2. RapidOCR로 텍스트 인식 (한자 우선, 일본어 가능)
  3. meta.json의 segments[i] 에 'text' 필드 추가
"""
import sys, os, json, subprocess, time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
BASE = Path(__file__).parent
OUT_DIR = BASE / 'output'

def get_ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

# Layout profiles: where the target text (Chinese/Japanese) is in the frame
PROFILES = {
    'oncn555':  {'y':(0.32,0.62), 'x':(0.02,0.98)},  # 영상 1 - 중앙 큰 한자
    'barooseo': {'y':(0.40,0.65), 'x':(0.02,0.98)},  # 영상 2 - 중앙 한자
    'default':  {'y':(0.30,0.70), 'x':(0.05,0.95)},  # 일반 (대부분 영상에 적용)
}

def detect_profile(meta):
    """Heuristic: pick profile from project id keywords."""
    pid = meta.get('id','').lower()
    if 'oncn' in pid: return PROFILES['oncn555']
    if 'baroo' in pid: return PROFILES['barooseo']
    return PROFILES['default']

def extract_frame(video, time_sec, out_jpg):
    cmd = [get_ffmpeg(), '-y', '-ss', str(time_sec), '-i', video,
           '-frames:v', '1', '-q:v', '3', out_jpg, '-loglevel', 'error']
    subprocess.run(cmd, capture_output=True)
    return os.path.exists(out_jpg)

def ocr_chunk(args):
    """Worker: OCR a chunk of segments."""
    video_path, segs, profile, worker_id = args
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    rapid = RapidOCR()

    import tempfile
    tmpdir = tempfile.mkdtemp(prefix=f'ocrw{worker_id}_')

    def crop(img, prof):
        H, W = img.shape[:2]
        y0, y1 = int(H*prof['y'][0]), int(H*prof['y'][1])
        x0, x1 = int(W*prof['x'][0]), int(W*prof['x'][1])
        return img[y0:y1, x0:x1]

    results = []
    for s in segs:
        mid = (s['start'] + s['end']) / 2
        frame_path = os.path.join(tmpdir, f's_{s["i"]}.jpg')
        if not extract_frame(video_path, mid, frame_path):
            results.append((s['i'], ''))
            continue
        try:
            arr = np.fromfile(frame_path, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                results.append((s['i'], '')); continue
            cropped = crop(img, profile)
            if cropped.size == 0:
                results.append((s['i'], '')); continue
            res, _ = rapid(cropped)
            if res:
                # pick the line with most CJK characters
                best_text, best_count = '', 0
                for item in res:
                    text = item[1]
                    cjk = sum(1 for c in text if '一' <= c <= '鿿' or 'ぁ' <= c <= 'ヶ')
                    if cjk > best_count:
                        best_count = cjk
                        best_text = text
                results.append((s['i'], best_text.strip()))
            else:
                results.append((s['i'], ''))
        except Exception as e:
            results.append((s['i'], ''))
        try: os.remove(frame_path)
        except: pass

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    return results

def enrich_project(pid, n_workers=4):
    meta_file = OUT_DIR / f'{pid}.meta.json'
    if not meta_file.exists():
        print(f'[{pid}] no meta'); return

    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    video_file = OUT_DIR / meta.get('video_file','')
    if not video_file.exists():
        print(f'[{pid}] video file missing: {video_file.name}'); return

    segs = meta.get('segments', [])
    if not segs:
        print(f'[{pid}] no segments'); return

    # Skip if already enriched
    if all(s.get('text') is not None for s in segs):
        print(f'[{pid}] already enriched ({len(segs)} segs)')
        # but check if any are non-empty
        non_empty = sum(1 for s in segs if s.get('text'))
        print(f'  → {non_empty} have text')

    profile = detect_profile(meta)
    print(f'[{pid}] enriching {len(segs)} segments (profile y={profile["y"]}) sequentially...')

    t0 = time.time()
    # Sequential single-process OCR (more stable on Windows)
    results = ocr_chunk((str(video_file), segs, profile, 0))
    text_map = dict(results)
    print(f'[{pid}] OCR done in {(time.time()-t0)/60:.1f}min')
    for s in segs:
        s['text'] = text_map.get(s['i'], '')

    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    non_empty = sum(1 for s in segs if s.get('text'))
    print(f'[{pid}] done: {non_empty}/{len(segs)} have text in {(time.time()-t0)/60:.1f}min')

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    n_workers = 4
    if '--workers' in sys.argv:
        i = sys.argv.index('--workers')
        n_workers = int(sys.argv[i+1])

    if '--all' in sys.argv:
        for meta_file in OUT_DIR.glob('*.meta.json'):
            pid = meta_file.stem.replace('.meta','')
            enrich_project(pid, n_workers)
    else:
        enrich_project(sys.argv[1], n_workers)

if __name__ == '__main__':
    main()
