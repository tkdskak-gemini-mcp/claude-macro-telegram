# Claude Macro Telegram

GitHub Actions로 투자 관련 이벤트를 텔레그램으로 자동 발송하는 시스템.

## 운영 중: 이벤트 알림 (`event-alerts.yml`, 2026-09-11~)

15분마다 아래를 확인해 **새 이벤트가 있을 때만** 텔레그램으로 **무음** 발송한다. 규칙 기반이라 Claude를 호출하지 않는다.

| 영역 | 소스 | 알림 기준 |
|---|---|---|
| 미국 공시 | SEC EDGAR | 보유·관심 종목의 8-K(항목 번호로 분류), 10-Q/10-K, 6-K, 증권 발행(S-3·424B), 13D, 공개매수·합병, 제출 지연(NT) |
| 한국 공시 | DART | 삼성전자·SK하이닉스 공시 (임원 지분변동 등 소음 제외) |
| 매크로 경보선 | Yahoo Finance | fed 4종(10Y 5.00%·USD/JPY 155·MOVE 90·닛케이 −15%), SGOV 트랜치(VIX 25/30/35·S&P −10/−15/−20%), 브렌트−WTI $10. 점등·해제 시 1회 |
| 매크로 급변 | Yahoo Finance | 금리·환율·VIX·지수·유가·가스·금의 하루 변동이 기준 초과 |
| 크레딧 | FRED | CCC÷HY 3배 점등·해제, HY +15bp·CCC +40bp 하루 급확대 |
| 예측시장 | Kalshi | FOMC·미이란 합의·호르무즈·대만·침체·AI IPO 확률이 24시간 내 ±10%p 급변 (OI 1만 이상) |
| 정책 | 백악관 발표·연방관보 | 관세·수출통제·제재·에너지 등 키워드가 들어간 대통령 조치, 상무부 BIS·USTR 문서 |

- 종목·기준값은 [alerts/config.json](alerts/config.json)에서 수정한다. **퍼블릭 저장소이므로 티커만 적고 주수·평단·금액은 적지 않는다.**
- 중복 방지 상태는 Actions 캐시에 저장한다. 첫 실행(또는 소스가 처음 성공한 실행)은 기존 이벤트를 '본 것'으로만 기록한다.
- 한 소스가 4회 연속(약 1시간) 실패하면 시스템 경고를 1회 보낸다.
- 수동 실행(Run workflow) 시 현재 경보선 현황 스냅샷을 함께 보낸다.
- 로컬 테스트: `python alerts/run.py --dry-run` (발송 없이 출력)

## 중단된 브리핑

| 워크플로 | 상태 |
|---|---|
| `macro-war.yml` (평일 07시) | 2026-09-11 자동발송 중단 — 이벤트 알림으로 대체. 수동 실행은 가능 |
| `macro-credit.yml` (평일 08시) | 2026-09-11 자동발송 중단 — 이벤트 알림으로 대체. 수동 실행은 가능 |
| `macro-trend.yml` (평일 09시) | 2026-07-16 자동발송 중단 |

재개하려면 해당 yml의 `schedule` 주석 3줄을 해제하고 push한다.

## GitHub Secrets

Repo Settings → Secrets and variables → Actions:

| 이름 | 용도 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급 |
| `TELEGRAM_CHAT_ID` | 본인 chat ID |
| `CLAUDE_CODE_OAUTH_TOKEN` | 브리핑 워크플로용 (`claude setup-token`) |
| `SEC_USER_AGENT` | SEC 접속용 연락처. 형식 `이름 (이메일)` — SEC는 연락처 없는 요청을 차단한다 |
| `DART_API_KEY` | OpenDART 인증키 |

`keepalive.yml`은 매월 1일 빈 커밋을 만들어, 퍼블릭 저장소의 예약 워크플로가 60일 비활성으로 꺼지는 것을 막는다.
