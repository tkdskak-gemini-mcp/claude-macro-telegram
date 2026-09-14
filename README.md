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
| 정책 | 백악관 발표·연방관보 | 관세·수출통제·제재·에너지 등 키워드가 들어간 대통령 조치, 상무부 BIS·USTR 문서. **정례 절차(의견요청·설명회·자료수집)는 ⚪로 낮춰 메시지 아래에 모으고, 기념일 포고문은 제외** |
| 판정일 | [alerts/thesis.json](alerts/thesis.json) | 날짜가 정해진 일정은 전날 08시 이후, "2026-11 초" 같은 대략 일정은 그 달 1·10·20일에 "볼 것"과 함께 |
| 핫이슈 | Bloomberg·FT·로이터·연준·OpenAI·Google AI·NBER·HF 논문 | 같은 사건을 매체 2곳 이상이 6시간 안에 다루고 포트폴리오 키워드에 걸릴 때 |

모든 알림 끝에는 **💬 붙여넣기 문장**이 붙는다. PC의 Claude Code에 그대로 붙이면 해당 프로젝트 트리거로 분석이 시작된다(연결표는 config의 `ask`).

### 핫이슈 요약 (Claude, 하루 2회)

KST 07시·18시 이후 첫 실행에서 [alerts/news.py](alerts/news.py)가 최근 14시간치 기사·논문을 모아
[prompts/news_digest.md](prompts/news_digest.md) 형식으로 요약한다. 판단 맥락은 [alerts/context.md](alerts/context.md)에서 읽는다(**수동 갱신 파일**).

- 유료 매체는 **제목과 짧은 요약까지만** 받을 수 있다 → 프롬프트에서 "목록 밖 내용 금지·헤드라인만 확보 표기"를 강제
- 목표가·투자의견 기사는 제외. 각 항목은 "무슨 일"이 아니라 **기존 가설을 강화/반증하는가**로 쓴다
- 논문은 즉시 알리지 않고 요약에서만 다룬다(추천수가 높아도 투자와 무관한 연구가 대부분)

### 실적 원문 채점 (Claude)

보유 종목의 실적 8-K(2.02)·10-Q·10-K·6-K, 관심 종목의 10-Q·10-K가 감지되면 채점 대기열에 넣는다.
[alerts/score.py](alerts/score.py)가 SEC 원문(본문+EX-99 보도자료)을 텍스트로 받고 XBRL 분기 시계열을 붙여,
Claude(`claude -p`, 도구는 Read·Grep·Glob만)가 [prompts/earnings_score.md](prompts/earnings_score.md) 형식으로
성장성 훼손 6대 신호와 thesis 체크를 채점해 별도 메시지로 보낸다.

- 실행 1회당 최대 2건 (실적 시즌 몰림 분산), 실패 시 3회 재시도 후 원문 링크와 함께 실패 알림
- 가격·밸류 지표는 쓰지 않고, 매수·매도 권고도 하지 않는다. 결론은 "③층 재검토 필요/불필요/보류"
- 로컬 확인: `python alerts/score.py --prepare-only` (Claude 호출 없이 원문·프롬프트만 준비)

- 종목·기준값은 [alerts/config.json](alerts/config.json)에서, 종목별 "볼 것"과 판정일은 [alerts/thesis.json](alerts/thesis.json)에서 수정한다. **퍼블릭 저장소이므로 티커만 적고 주수·평단·금액은 적지 않는다.**
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
