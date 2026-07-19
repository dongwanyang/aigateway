#!/usr/bin/env bash
# 控制台聊天窗 MVP —— 后端 SSE 形态烟测。
# 用法: ADMIN_KEY=xxx GATEWAY=http://localhost:8000 bash scripts/smoke_chat.sh
set -euo pipefail

ADMIN_KEY="${ADMIN_KEY:?需要 ADMIN_KEY 环境变量}"
GATEWAY="${GATEWAY:-http://localhost:8000}"

echo "==> 文本意图 SSE 烟测"
text_resp=$(curl -sN -X POST "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"你好"}]}' || true)
echo "$text_resp" | grep -q '"delta"' || { echo "FAIL: 文本 SSE 未出现 delta"; exit 1; }
echo "$text_resp" | grep -q '\[DONE\]' || { echo "FAIL: 未收到 [DONE]"; exit 1; }
echo "PASS: 文本 SSE 形态正常"

echo "==> 图片意图 SSE 烟测"
img_resp=$(curl -sN -X POST "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"画一只猫"}]}' || true)
echo "$img_resp" | grep -q '"generation:image"' || { echo "WARN: 未检测到 generation:image intent(可能后端分类未命中,人工确认)"; }
echo "$img_resp" | grep -qE '"content":"https?://|"content":"data:image/' \
  && echo "PASS: 图片 content 是 URL/b64" \
  || echo "WARN: 图片 content 非预期 URL/b64 形态(人工确认)"

echo "==> 全部烟测完成"
