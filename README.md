# 통합 플레이어 (Repeat Player)

언어 학습용 동영상 반복 플레이어. 영상을 자동으로 문장 단위로 분리해서 무한 반복 재생, 자막 캡처 후 OCR + AI로 한자/일본어/중국어/영어/베트남어 자동 분석.

## 주요 기능
- **자동 문장 분리**: ffmpeg silencedetect로 무음 구간 감지 → 문장별 세그먼트 생성
- **무한 반복**: 1x ~ ∞ 반복 횟수 설정, 자동 다음 문장 진행
- **자막 OCR**: 영상의 한 프레임 캡처 → RapidOCR로 텍스트 추출
- **AI 해석**: OpenRouter (Gemini/Llama) 무료 모델로 한국어 번역 + 발음 + 단어 풀이
- **외부 사전 연결**: 네이버 사전 (中-韓 / 日-韓 / 越-韓 / 英-韓) 자동 라우팅
- **세그먼트별 학습 캐시**: 분석 결과가 세그먼트마다 자동 저장/복원
- **반응형 UI**: PC + 모바일(폰 가로 한 줄에 모든 컨트롤)

## 설치

```bash
pip install -r requirements.txt
# 또는 개별 설치:
pip install rapidocr-onnxruntime opencv-python imageio-ffmpeg yt-dlp
```

Python 3.10+ 권장.

## 사용

```bash
# 1. 서버 시작
서버시작.bat   # (Windows)
# 또는
python server.py

# 2. 브라우저에서 열기
PC : http://localhost:5757/
폰 : http://<LAN_IP>:5757/   (서버 시작 시 콘솔에 표시)
```

## 영상 추가
1. **로컬 파일 드래그**: 메인 화면에 mp4/mkv/webm 드래그
2. **YouTube URL** (옵션): `+ 추가` 버튼 → URL 입력
   - 본인 권한 있는 영상 또는 사적 학습용으로만 사용

## 두 가지 사용 모드 (자동 분기)

키 유무에 따라 화면 자동 변경:

| 모드 | 키 | 사용 가능 기능 |
|------|----|----|
| **기본** | 없음 | 자막 캡처(OCR) + 사전(내장) + 네이버(외부 링크) |
| **AI 확장** | OpenRouter 키 | 위 + 🤖 AI 분석(번역/병음/글자별 풀이) |

### AI 키 추가 (선택)
1. https://openrouter.ai/keys 에서 무료 가입
2. 무료 모델 (`gemini-2.5-flash-lite` 등) 사용 가능
3. 플레이어 우상단 [설정] → [OpenRouter 키 입력] 클릭 → 키 붙여넣기
4. AI 버튼 자동 활성화

키를 입력하지 않아도 사전+네이버만으로 충분히 학습 가능합니다.

## 데이터 저장 위치
- 영상/자막/메타: `output/<프로젝트ID>.{mp4,segments.json,meta.json}`
- API 키: `openrouter.key` (텍스트 파일)
- 세그먼트별 분석 결과: 브라우저 localStorage

서버는 **로컬 전용**입니다. 외부에 데이터를 전송하지 않습니다 (단, AI 해석 시 OpenRouter API에만 전송).

## 면책

- 이 프로그램은 **언어 학습을 위한 개인용 도구**입니다
- 본인이 권리를 가진 영상 또는 권리자의 허락을 받은 영상만 사용하세요
- YouTube 영상 다운로드는 각국 저작권법과 YouTube 약관을 준수해야 합니다 (한국 저작권법 §30 사적 이용 범위 내)
- OpenRouter API는 본인 계정/키로 사용하세요 (사용료 본인 부담)
- 이 프로그램으로 인한 손해/법적 책임은 사용자 본인에게 있습니다

## 라이선스

MIT License — 자유롭게 사용/수정/재배포 가능. `LICENSE` 파일 참조.

## 주요 의존성 라이선스
| 패키지 | 라이선스 |
|--------|---------|
| yt-dlp | Unlicense (퍼블릭 도메인) |
| ffmpeg (imageio-ffmpeg) | LGPL/GPL |
| rapidocr-onnxruntime | Apache 2.0 |
| opencv-python | Apache 2.0 |
| Pillow | MIT-CMU |

## 문제 신고 / 기여

이슈/PR 환영합니다.
