#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  printf '请使用 sudo 运行: sudo %s\n' "$0" >&2
  exit 1
fi

repo_root=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      [[ $# -ge 2 ]] || { echo "--repo-root 缺少值" >&2; exit 1; }
      repo_root="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$repo_root" ]] || repo_root="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$repo_root" && pwd)"
controller="$repo_root/scripts/gpu-topology-controller.py"
[[ -f "$controller" ]] || { echo "未找到控制器: $controller" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "未找到 python3" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "未找到 docker" >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "当前系统不支持 systemd" >&2; exit 1; }
service_user="${SUDO_USER:-$(stat -c '%U' "$repo_root")}"
[[ "$service_user" != "root" ]] || service_user="$(stat -c '%U' "$repo_root")"
service_group="$(id -gn "$service_user")"
docker_group="$(stat -c '%G' /var/run/docker.sock)"

unit=/etc/systemd/system/aigateway-gpu-topology.service
temporary="$(mktemp /etc/systemd/system/.aigateway-gpu-topology.XXXXXX)"
trap 'rm -f "$temporary"' EXIT
{
  echo '[Unit]'
  echo 'Description=AI Gateway GPU topology reconciler'
  echo 'After=docker.service network-online.target'
  echo 'Requires=docker.service'
  echo
  echo '[Service]'
  echo 'Type=simple'
  printf 'User=%s\n' "$service_user"
  printf 'Group=%s\n' "$service_group"
  printf 'SupplementaryGroups=%s\n' "$docker_group"
  printf 'WorkingDirectory=%s\n' "$repo_root"
  printf 'ExecStart=/usr/bin/python3 %s --repo-root %s --watch\n' "$controller" "$repo_root"
  echo 'Restart=on-failure'
  echo 'RestartSec=5'
  echo 'NoNewPrivileges=true'
  echo
  echo '[Install]'
  echo 'WantedBy=multi-user.target'
} > "$temporary"
chmod 0644 "$temporary"
mv "$temporary" "$unit"
trap - EXIT

systemctl daemon-reload
systemctl enable --now aigateway-gpu-topology.service
systemctl --no-pager --full status aigateway-gpu-topology.service || true
printf 'GPU 拓扑自动控制器已启用。\n'
