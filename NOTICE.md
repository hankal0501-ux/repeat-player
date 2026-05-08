# 의존성 라이선스 고지

이 프로그램은 다음 오픈소스 라이브러리를 사용합니다.
각 라이브러리의 라이선스 전문은 해당 패키지에 포함되어 있습니다.

## Python 패키지

| 패키지 | 라이선스 | 출처 |
|--------|---------|------|
| rapidocr-onnxruntime | Apache License 2.0 | https://github.com/RapidAI/RapidOCR |
| opencv-python | Apache License 2.0 | https://github.com/opencv/opencv-python |
| imageio-ffmpeg | BSD 2-Clause | https://github.com/imageio/imageio-ffmpeg |
| numpy | BSD 3-Clause | https://numpy.org |
| Pillow | MIT-CMU | https://python-pillow.org |
| yt-dlp | Unlicense (퍼블릭 도메인) | https://github.com/yt-dlp/yt-dlp |

## 번들된 바이너리

- **ffmpeg** (imageio-ffmpeg를 통해 자동 다운로드): LGPL 2.1+ / GPL 2+
  - 출처: https://ffmpeg.org
  - LGPL 의무 사항: 본 프로그램은 ffmpeg를 동적으로 호출만 하며, 수정하지 않습니다.

## 외부 서비스

| 서비스 | 약관 |
|--------|------|
| OpenRouter API | https://openrouter.ai/terms |
| Google Gemini (via OpenRouter) | https://ai.google.dev/terms |
| 네이버 사전 (단순 링크 연결) | https://dict.naver.com |
| YouTube (URL 입력 시) | https://www.youtube.com/t/terms |

## 모델

- **RapidOCR PP-OCRv4** (포함된 ONNX 모델): Apache License 2.0
  - 출처: https://github.com/RapidAI/RapidOCR

---

본 고지는 정보 제공 목적이며, 정확한 라이선스 의무는 각 패키지의 LICENSE 파일을 참조하세요.
