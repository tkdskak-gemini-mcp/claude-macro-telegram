#!/bin/bash
# 10/23: GitHub 예약 실행이 15분 설계인데 실제 간격 중앙값 4.5시간(최대 10시간) → 자비스 서버(Lightsail)의 systemd 타이머로 이전.
# 텔레그램은 뉴스봇 자기 토큰으로 보낸다(자비스와 분리). 비밀은 /home/kiwoombot/newsbot-data/.secrets (LP가 직접 기입).
set -u
D=/home/kiwoombot/newsbot-data
R=/home/kiwoombot/claude-macro-telegram
J=/home/kiwoombot/kiwoom-telegram-bot/.env
# 값에 띄어쓰기가 있어도(SEC_USER_AGENT 등) 깨지지 않게 한 줄씩 읽는다 — `. 파일`(source)로 읽으면 공백 뒤가 명령으로 실행됨
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue;; esac
  k="${line%%=*}"; v="${line#*=}"; v="${v%$''}"
  [ -n "$v" ] && export "$k=$v"
done < "$D/.secrets"
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then echo "뉴스봇 토큰 미설정 — 건너뜀"; exit 0; fi
# 채팅 ID를 따로 적지 않았으면 LP 개인 채팅(자비스 허용 목록의 첫 번째)으로 보낸다 — 개인 채팅의 chat_id 는 사용자 ID라 봇이 달라도 같다
[ -n "${TELEGRAM_CHAT_ID:-}" ] || export TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_ALLOWED_CHAT_IDS=' "$J" | cut -d= -f2 | cut -d, -f1 | tr -d ' ')
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || export CLAUDE_CODE_OAUTH_TOKEN=$(grep '^CLAUDE_CODE_OAUTH_TOKEN=' "$J" | cut -d= -f2-)
export ALERT_STATE="$D/state.json" JARVIS_SNAP=/home/kiwoombot/kiwoom-telegram-bot/data/snapshots.db PATH="/home/kiwoombot/.local/bin:$PATH" PYTHONIOENCODING=utf-8
git -C "$R" pull -q --ff-only || echo "git pull 실패 — 기존 코드로 계속"
OUT=$(mktemp); export GITHUB_OUTPUT="$OUT"
cd "$R"
python3 alerts/run.py "$@"
if grep -q '^news=true' "$OUT"; then python3 alerts/news.py || echo "핫이슈 요약 실패(다음 실행에서 재시도)"; fi
if grep -q '^score=true' "$OUT"; then python3 alerts/score.py || echo "실적 채점 실패"; fi
rm -f "$OUT"
