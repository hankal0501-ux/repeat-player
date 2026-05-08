# 가족 공유용 클라우드 배포 (Render 무료)

폰만으로 어디서나 풀기능 사용. 가족/친구 몇 명 공유.

## 1. 사전 준비 (1회)

### GitHub 계정 + repo
```bash
cd e:\역사바로세우기\repeat_player
git init
git add .
git commit -m "Initial"
gh repo create repeat-player --public --source=. --push
# 또는 GitHub.com에서 수동 생성 후 git push
```

### Render 가입
- https://render.com → GitHub 계정으로 로그인 (무료)
- 신용카드 등록 불필요

## 2. 배포 (10분)

### 옵션 A: Blueprint (가장 쉬움)
1. https://dashboard.render.com/blueprints/new
2. GitHub repo 연결 → repeat-player 선택
3. `render.yaml` 자동 인식
4. **환경변수 입력** 화면에서:
   - `RP_USER` = 원하는 아이디 (예: `family`)
   - `RP_PASS` = 원하는 비밀번호 (예: `mySecret123!`)
5. "Apply" 클릭 → 5-10분 빌드

### 옵션 B: 수동
1. https://dashboard.render.com/web/new
2. "Build and deploy from a Git repository" 선택
3. repo 선택
4. 설정:
   - Name: `repeat-player`
   - Environment: `Docker`
   - Branch: `main`
   - Plan: **Free**
5. **Environment Variables**:
   - `RP_USER` / `RP_PASS` 추가
6. **Disks** → Add Disk:
   - Name: `data`
   - Mount Path: `/data`
   - Size: 1GB
7. "Create Web Service" 클릭

## 3. 사용

배포 완료 후 Render가 URL 부여:
```
https://repeat-player.onrender.com   (이름은 본인 service 이름에 따라)
```

가족에게 전달:
```
주소: https://repeat-player.onrender.com
ID  : family
PW  : mySecret123!
```

폰 브라우저에서 접속 → 첫 방문 시 로그인 팝업 → 이후 자동 인증.

## 4. 무료 티어 주의사항

| 항목 | 무료 한도 | 영향 |
|------|----------|------|
| 시간 | 750h/월 | 항상 켜둬도 됨 |
| 슬립 | 15분 비활성 시 | 첫 요청 30초 콜드 스타트 |
| 디스크 | 1GB | 영상 ~3-5개 저장 가능 |
| 트래픽 | 무제한 | 영상 스트리밍 OK |
| 빌드 | 500분/월 | 충분 |

**슬립 회피 팁**:
- UptimeRobot 무료 (https://uptimerobot.com): 5분 간격 헬스체크 → 슬립 안 함
- 또는 그냥 콜드 스타트 받아들이기 (가족용이면 30초 OK)

## 5. 영상 업로드

배포된 사이트에서 평소처럼:
1. PC에서 https://repeat-player.onrender.com 접속
2. 영상 파일 드래그 또는 YouTube URL 입력
3. 처리 (몇 분 소요 — 무료 티어 CPU 느림)
4. 폰에서 같은 URL → 처리된 영상 재생

## 6. 보안 고려사항

| 항목 | 설명 |
|------|------|
| **비밀번호** | RP_USER/RP_PASS로 인증, 가족만 사용 |
| **HTTPS** | Render가 자동 (Let's Encrypt) |
| **업로드 크기** | 무료 티어 256MB 메모리 → 큰 영상 처리 시 OOM |
| **저작권** | 본인 영상만 업로드 (재배포 X) |

## 7. 비용

- **무료 티어**: 0원 (제한적)
- **Starter ($7/월)**: 슬립 없음, 512MB RAM
- **Standard ($25/월)**: 2GB RAM (큰 영상 OK)

가족용이면 무료로 충분합니다.

## 8. 업데이트

코드 수정 후:
```bash
git add . && git commit -m "update" && git push
```
→ Render 자동 재배포 (3-5분)

## 9. 삭제

Render 대시보드 → Settings → Delete Service.
