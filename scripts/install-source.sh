#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

for argument in "$@"; do
  case "$argument" in
    --profile|runtime|rag|vision)
      echo "旧源码 profile 已移除；请使用 --edition lite|knowledge|studio|full。" >&2
      exit 1
      ;;
  esac
done

exec "$repo_root/scripts/quickstart.sh" --distribution source "$@"
