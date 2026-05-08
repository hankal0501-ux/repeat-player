# Windows 실행파일 (.exe) 빌드

비개발자 사용자를 위해 Python 설치 없이 실행 가능한 단일 실행파일 만드는 방법.

## 방법 1: PyInstaller (가장 간단)

### 설치
```bash
pip install pyinstaller
```

### 빌드
```bash
pyinstaller --onefile --name "RepeatPlayer" --hidden-import=rapidocr_onnxruntime ^
            --hidden-import=cv2 --hidden-import=imageio_ffmpeg ^
            --add-data "openrouter.key;." ^
            server.py
```

결과: `dist/RepeatPlayer.exe` (약 120-200 MB — onnxruntime/opencv 포함)

### 빌드 스크립트 (`build.bat`)
```batch
@echo off
pyinstaller --onefile --name RepeatPlayer ^
  --icon=icon.ico ^
  --hidden-import=rapidocr_onnxruntime ^
  --hidden-import=cv2 ^
  --hidden-import=imageio_ffmpeg ^
  --hidden-import=onnxruntime ^
  --collect-all=rapidocr_onnxruntime ^
  --collect-all=imageio_ffmpeg ^
  server.py
echo.
echo === 빌드 완료 ===
echo 결과: dist\RepeatPlayer.exe
pause
```

## 방법 2: Nuitka (빠른 실행)

```bash
pip install nuitka
python -m nuitka --onefile --include-package=rapidocr_onnxruntime ^
                 --include-package=cv2 server.py
```

## 배포 패키지 구조

```
RepeatPlayer-v1.0/
├── RepeatPlayer.exe        # 실행파일
├── README.md               # 사용법
├── LICENSE                 # MIT 라이선스
├── NOTICE.md               # 의존성 고지
└── output/                 # 빈 폴더 (사용자 영상용)
```

ZIP으로 압축 → 사용자에게 배포.

## 사용자 안내 (README 별도 추가)

```
1. 압축 해제
2. RepeatPlayer.exe 더블클릭 (Windows Defender 경고 나오면 [추가 정보] → [실행])
3. 브라우저가 자동으로 안 열리면 직접: http://localhost:5757/
4. 영상 파일 드래그해서 학습 시작
```

## 코드 사이닝 (선택, 유료)

Windows Defender / SmartScreen 경고 없이 실행되려면 EV 코드 사이닝 인증서 필요 (~연 200 USD).
무료 배포면 사용자에게 [추가 정보] → [실행] 클릭하라고 안내.

## 크로스 플랫폼

- **Mac**: `pyinstaller --onefile server.py` (macOS .app은 추가 작업 필요)
- **Linux**: 동일하게 `pyinstaller` 사용 (단일 binary)

## 자동 업데이트 (옵션)

GitHub Releases에 .exe 업로드 → 프로그램 시작 시 GitHub API로 최신 버전 체크 (구현은 별도 작업).
