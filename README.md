# SK Report Bot

매일 아침 SK하이닉스(반도체·메모리) + 반도체/IT서비스·AI 산업동향 증권사 리포트를 모아
텔레그램으로 보내주는 자동화 봇입니다. GitHub Actions에서 매일 07:00(KST)에 실행됩니다.

Anthropic API 키를 등록하면 상위 리포트 3개의 PDF 본문을 서버가 직접 추출해,
SK하이닉스 양산관리와 SK AX 두 직무 트랙의 초보자 해석·실제 근거·직무 연결·
필수 개념·상황형 면접 연습으로 정리해 함께 보내줍니다
(없어도 리포트 제목/증권사/링크는 정상적으로 옵니다).
텍스트 추출이 어려운 차트 중심 PDF는 Claude의 PDF 문서 분석으로 자동 보완합니다.

## 1. 저장소 준비

1. GitHub에서 새 저장소를 만들고(Private 추천), 이 폴더의 파일들을 업로드/push 합니다.
   ```
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin <내 저장소 URL>
   git push -u origin main
   ```

## 2. Secrets 등록

저장소 `Settings > Secrets and variables > Actions > New repository secret` 에서 아래를 추가합니다.

| Name | 값 | 필수 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 (BotFather에서 발급) | 필수 |
| `TELEGRAM_CHAT_ID` | 메시지를 받을 chat id (아래 3번 참고) | 필수 |
| `ANTHROPIC_API_KEY` | Anthropic API 키 (console.anthropic.com) | 선택 |

## 3. Chat ID 구하는 법

1. 텔레그램에서 내가 만든 봇을 검색해 아무 메시지나 하나 보냅니다 (예: "hi").
2. 브라우저에서 아래 주소에 접속합니다 (`<TOKEN>` 자리에 실제 봇 토큰을 넣기):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. 응답 JSON에서 `"chat":{"id": 123456789, ...}` 부분의 숫자가 chat id입니다.

## 4. Actions 활성화 및 테스트

1. 저장소 `Actions` 탭에서 워크플로 사용을 허용합니다.
2. `Actions > Daily SK Report > Run workflow` 로 수동 실행해 봅니다 (workflow_dispatch).
3. 실행 로그(Actions 탭 > 해당 run)에서 에러가 없는지, 텔레그램 메시지가 도착했는지 확인합니다.
4. 정상 확인되면 이후 매일 07:00(KST)에 자동으로 실행됩니다.

## 5. 커스터마이징

- `main.py` 상단의 `INDUSTRY_KEYWORDS` 리스트를 수정하면 다루는 키워드 범위를 넓히거나 좁힐 수 있습니다.
- `LOOKBACK_DAYS` 값으로 며칠 이내 리포트까지 포함할지 조절합니다 (기본 3일, 주말/휴장 대비).
- `.github/workflows/daily-report.yml`의 cron 값을 바꾸면 발송 시각을 바꿀 수 있습니다.
  (cron은 UTC 기준이므로 KST 기준 시각에서 9시간을 빼서 입력)

## 6. 로컬 테스트

```bash
pip install -r requirements.txt
export DRY_RUN=1
python main.py
```
`DRY_RUN=1`이면 텔레그램 전송 없이 콘솔에 메시지 내용만 출력합니다.

## 참고 / 한계

- 네이버금융(finance.naver.com) 페이지 구조를 스크래핑하는 방식이라, 네이버가 페이지 구조를 바꾸면
  파싱이 깨질 수 있습니다. 이 경우 Actions 실행 로그의 에러 메시지를 보고 `main.py`의
  `parse_research_table` 함수를 수정해야 합니다.
- 투자 자문이나 매매 신호를 제공하는 목적이 아니라, 산업 동향 파악/면접 준비용 정보 수집 도구입니다.
