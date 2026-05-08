"""
일괄 영상 처리.

사용법:
  1. urls.txt 파일에 한 줄에 하나씩 적기:
       <id> <url> [--mode both|download|stream] [--noise -30] [--silence 0.4]
  2. python batch.py

기능:
  - 이미 처리된 ID는 자동 스킵 (output/<id>.html 존재 시)
  - 오류나도 다음 영상 계속 진행
  - 마지막에 결과 요약

예시 urls.txt:
    # 주석 가능
    v4_test1 https://www.youtube.com/watch?v=AAA
    v5_lesson https://www.youtube.com/watch?v=BBB --silence 0.3
    v6_jp https://www.youtube.com/watch?v=CCC --noise -25
"""
import sys, subprocess, time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

BASE = Path(__file__).parent
URLS_FILE = BASE / 'urls.txt'
OUTPUT_DIR = BASE / 'output'
MAKE = BASE / 'make_player.py'

def log(m):
    print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)

def main():
    if not URLS_FILE.exists():
        log(f'Creating template: {URLS_FILE}')
        URLS_FILE.write_text(
            '# 한 줄에 하나씩: <id> <url> [optional flags]\n'
            '# 예시:\n'
            '# v4_test https://www.youtube.com/watch?v=AAA\n'
            '# v5_jp   https://www.youtube.com/watch?v=BBB --silence 0.3\n',
            encoding='utf-8')
        log('Edit urls.txt and run again.')
        return

    lines = URLS_FILE.read_text(encoding='utf-8').splitlines()
    tasks = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = line.split()
        if len(parts) < 2:
            log(f'  ⚠ skipping malformed: {line}')
            continue
        sid, url = parts[0], parts[1]
        extra_args = parts[2:]
        tasks.append({'id': sid, 'url': url, 'extra': extra_args})

    if not tasks:
        log('urls.txt에 처리할 항목이 없습니다.')
        return

    log(f'=== 총 {len(tasks)}개 항목 처리 ===')
    results = {'done': [], 'skip': [], 'fail': []}
    t0 = time.time()

    for i, task in enumerate(tasks, 1):
        sid = task['id']
        url = task['url']
        html_out = OUTPUT_DIR / f'{sid}.html'

        log(f'\n[{i}/{len(tasks)}] {sid}  ({url})')

        if html_out.exists():
            log(f'  ⊘ 이미 처리됨 (output/{sid}.html), 스킵')
            results['skip'].append(sid)
            continue

        cmd = [sys.executable, str(MAKE), url, sid] + task['extra']
        try:
            r = subprocess.run(cmd, cwd=str(BASE))
            if r.returncode == 0:
                log(f'  ✓ 완료')
                results['done'].append(sid)
            else:
                log(f'  ✗ 실패 (exit {r.returncode})')
                results['fail'].append(sid)
        except Exception as e:
            log(f'  ✗ 예외: {e}')
            results['fail'].append(sid)

    log(f'\n=== 완료 ({(time.time()-t0)/60:.1f}분) ===')
    log(f'  처리: {len(results["done"])} | 스킵: {len(results["skip"])} | 실패: {len(results["fail"])}')
    if results['done']:  log(f'  ✓ {", ".join(results["done"])}')
    if results['skip']:  log(f'  ⊘ {", ".join(results["skip"])}')
    if results['fail']:  log(f'  ✗ {", ".join(results["fail"])}')

if __name__ == '__main__':
    main()
