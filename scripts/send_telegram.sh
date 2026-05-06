#!/usr/bin/env bash
# Send a message to Telegram via Bot API
# Usage:
#   echo "msg" | ./send_telegram.sh
#   ./send_telegram.sh "msg"
#   ./send_telegram.sh < file.txt
#
# Env vars required:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID

set -euo pipefail

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars required" >&2
  exit 1
fi

if [ $# -gt 0 ]; then
  MESSAGE="$1"
else
  MESSAGE="$(cat)"
fi

if [ -z "${MESSAGE//[[:space:]]/}" ]; then
  echo "Error: empty message" >&2
  exit 1
fi

# Telegram message hard limit is 4096 chars; split if needed
send_chunk() {
  local chunk="$1"
  local payload
  payload="$(jq -n \
    --arg chat_id "$TELEGRAM_CHAT_ID" \
    --arg text "$chunk" \
    '{chat_id: $chat_id, text: $text, disable_web_page_preview: true}')"

  local response
  response="$(curl -sS -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$payload")"

  local ok
  ok="$(echo "$response" | jq -r '.ok')"
  if [ "$ok" != "true" ]; then
    echo "Telegram API error: $response" >&2
    return 1
  fi
}

# Split by 3500 chars per chunk (under 4096 limit, leaves margin)
CHUNK_SIZE=3500
LEN=${#MESSAGE}
START=0
while [ $START -lt $LEN ]; do
  CHUNK="${MESSAGE:$START:$CHUNK_SIZE}"
  send_chunk "$CHUNK"
  START=$((START + CHUNK_SIZE))
done

echo "Sent successfully ($LEN chars)"
