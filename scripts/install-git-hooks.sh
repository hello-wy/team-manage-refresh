#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

test -x .githooks/post-commit
git config core.hooksPath .githooks
printf '%s\n' "已启用项目 Git hooks：提交 main 后会自动 push 并触发 Netcup 部署。"
