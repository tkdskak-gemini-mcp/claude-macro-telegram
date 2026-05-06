# Claude Macro Telegram

GitHub Actions로 Claude Max Plan을 활용해 매크로 리스크 분석을 텔레그램으로 자동 발송하는 시스템.

## Architecture

```
[Cron Schedule]
     ↓
[GitHub Actions Runner]
     ↓
[Claude Code Action] ← OAuth Token (Max Plan)
     ↓
[Analysis Result]
     ↓
[Telegram Bot API] → 사용자 텔레그램
```

## Triggers (예정)

| 트리거 | 발송 주기 | 내용 |
|---|---|---|
| war | 매주 월요일 | 글로벌 전쟁 리스크 (Polymarket·WTI·VIX·대만) |
| fed | 매주 월요일 | 연준 금리·경기침체 리스크 (CME FedWatch·스프레드) |
| ai | 격주 | AI·반도체 사이클 (SOX·NVDA·빅테크 capex) |
| credit | 매주 월요일 | 신용·금융 시스템 스트레스 (HY·MOVE·은행권) |

## Setup

### 1. Required GitHub Secrets

Repo Settings → Secrets and variables → Actions:

- `TELEGRAM_BOT_TOKEN` — BotFather에서 발급
- `TELEGRAM_CHAT_ID` — @userinfobot에서 확인
- `CLAUDE_CODE_OAUTH_TOKEN` — `claude setup-token`으로 발급

### 2. Manual Test

Actions 탭 → "Manual Test - Telegram Send" → Run workflow

텔레그램으로 `Hello from GitHub Actions!` 도착 확인.

## Status

- [x] 텔레그램 발송 스크립트
- [x] 수동 테스트 워크플로우
- [ ] Claude OAuth 통합
- [ ] 트리거별 분석 프롬프트
- [ ] Cron 자동 스케줄링
