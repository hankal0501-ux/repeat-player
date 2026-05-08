"""
영상 무음구간 감지 → 문장 경계 추출.

ffmpeg silencedetect로 무음 구간 찾기 → 그 사이가 문장(speech).

사용법:
  python detect_segments.py <video.mp4> [output.json] [min_silence_sec=0.4] [noise_db=-30]

출력 JSON:
  [{"i": 0, "start": 1.5, "end": 4.2}, ...]
"""
import sys, subprocess, re, json
from pathlib import Path

if len(sys.argv) < 2:
    print(__doc__); sys.exit(1)

VIDEO = sys.argv[1]
OUT   = sys.argv[2] if len(sys.argv) > 2 else str(Path(VIDEO).with_suffix('.segments.json'))
MIN_SILENCE_DUR = float(sys.argv[3]) if len(sys.argv) > 3 else 0.4
NOISE_DB = int(sys.argv[4]) if len(sys.argv) > 4 else -30

def get_ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def get_duration(video):
    cmd = [get_ffmpeg(), '-i', video, '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True)
    stderr = (r.stderr or b'').decode('utf-8', errors='replace')
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', stderr)
    if m: return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    return 0

def detect(video, min_dur, noise_db):
    print(f'Analyzing {video} (min_silence={min_dur}s, noise={noise_db}dB)...')
    cmd = [get_ffmpeg(), '-i', video,
           '-af', f'silencedetect=noise={noise_db}dB:d={min_dur}',
           '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True)
    stderr = (r.stderr or b'').decode('utf-8', errors='replace')

    starts = [float(m.group(1)) for m in re.finditer(r'silence_start: ([\d.]+)', stderr)]
    ends   = [float(m.group(1)) for m in re.finditer(r'silence_end: ([\d.]+)', stderr)]
    print(f'  silence events: {len(starts)} starts, {len(ends)} ends')

    duration = get_duration(video)
    print(f'  video duration: {duration:.1f}s')

    # Build speech segments: the gaps between silences
    # Audio starts speaking at t=0 OR at silence_end
    # Audio ends speaking at silence_start OR at duration
    events = [(s, 'silence_start') for s in starts] + [(e, 'silence_end') for e in ends]
    events.sort()

    segments = []
    speech_start = 0.0  # assume speech starts at t=0
    in_silence = False  # if first event is silence_start, we WERE speaking from 0

    # If first event is silence_start, speech was 0 → starts[0]
    # If first event is silence_end, that means video began with silence
    if events and events[0][1] == 'silence_end':
        speech_start = events[0][0]  # speech begins after first silence
        events = events[1:]

    for t, typ in events:
        if typ == 'silence_start':
            # speech ends here
            if t - speech_start > 0.3:  # min sentence length
                segments.append({'start': round(speech_start, 2), 'end': round(t, 2)})
            in_silence = True
        else:  # silence_end
            speech_start = t
            in_silence = False

    # Tail: if last event was silence_end, there's speech until duration
    if not in_silence and duration - speech_start > 0.3:
        segments.append({'start': round(speech_start, 2), 'end': round(duration, 2)})

    # Number them
    for i, s in enumerate(segments):
        s['i'] = i
        s['dur'] = round(s['end'] - s['start'], 2)

    return segments, duration

def main():
    segments, duration = detect(VIDEO, MIN_SILENCE_DUR, NOISE_DB)
    out_data = {
        'video': str(Path(VIDEO).name),
        'duration': duration,
        'min_silence_dur': MIN_SILENCE_DUR,
        'noise_db': NOISE_DB,
        'segments': segments,
        'count': len(segments),
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)
    print(f'Detected {len(segments)} segments → {OUT}')
    if segments:
        avg_dur = sum(s['dur'] for s in segments) / len(segments)
        print(f'  avg sentence: {avg_dur:.1f}s, total speech: {sum(s["dur"] for s in segments):.0f}s of {duration:.0f}s')
        print('  first 5:')
        for s in segments[:5]:
            print(f'    #{s["i"]}: {s["start"]:.1f}-{s["end"]:.1f}s ({s["dur"]:.1f}s)')

if __name__ == '__main__':
    main()
