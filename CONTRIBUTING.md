# 기여 가이드

## 시작하기

```bash
git clone <repo>
cd repeat_player
pip install -r requirements.txt
python server.py
```

브라우저: http://localhost:5757/

## 코드 구조

```
repeat_player/
├── server.py            # 서버 + HTML/CSS/JS (단일 파일, 모놀리식)
├── make_player.py       # YouTube 다운로드 → 무음 감지 → meta 생성
├── detect_segments.py   # ffmpeg silencedetect 파서
├── enrich_segments.py   # OCR 일괄 처리 (옵션)
├── requirements.txt
├── output/              # 사용자 영상/메타 (gitignore)
└── openrouter.key       # 사용자 API 키 (gitignore)
```

`server.py`가 1개 파일에 모든 것을 담고 있습니다 (HTTP 핸들러 + INDEX_HTML 문자열 + JS).
- 라인 ~290: CSS
- 라인 ~470: HTML 본문
- 라인 ~600: JS (플레이어 로직)
- 라인 ~1000: HANJA_DICT / WORD_DICT / JP_DICT
- 라인 ~1700: 분석 함수
- 라인 ~1900: 서버 라우팅

## 기여 환영 영역

- **사전 확장**: HANJA_DICT, WORD_DICT, JP_DICT 항목 추가
- **언어 감지 개선**: detectLang() 정확도 (특히 한자/중국어 구분)
- **OCR 정확도**: PROFILES 좌표 추가 (영상 종류별 텍스트 위치)
- **모바일 UX**: 폰 가로/세로 모드 대응
- **AI 모델 폴백**: FREE_TEXT_MODELS / FREE_VISION_MODELS 업데이트

## PR 규칙

1. 1개 PR = 1개 변경 (작게)
2. 커밋 메시지: 한글 OK, 80자 이내 요약
3. 테스트: 영상 1개 처리 → 자막 캡처 → 사전/AI 모두 동작 확인
4. 의존성 추가는 별도 이슈 먼저

## 보안

- `openrouter.key`를 절대 커밋하지 마세요 (`.gitignore`에 이미 포함)
- API 키 하드코딩 금지
- `output/` 폴더의 사용자 영상 커밋 금지

## 라이선스

기여한 코드는 MIT 라이선스로 배포됩니다.
