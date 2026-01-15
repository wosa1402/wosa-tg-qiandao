#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
用途：
  构建“干净镜像”：只打包项目源码与必要文件，避免把本地运行痕迹（session/log/db 等）打进镜像。

用法：
  scripts/docker_build_clean.sh [web|cli] [image:tag]

示例：
  scripts/docker_build_clean.sh web tg-signer-web:latest
  scripts/docker_build_clean.sh cli tg-signer:latest

可选环境变量：
  TZ=Asia/Shanghai     构建时区（传给 Dockerfile 的 TZ build-arg）
  REF=HEAD             当使用 git archive 时打包的引用（分支/标签/提交），默认 HEAD
  USE_GIT_ARCHIVE=1    是否优先用 git archive 生成“仅含受 Git 追踪文件”的构建上下文（推荐），默认 1
  NO_CACHE=0           设为 1 则 docker build 加 --no-cache
  PIP_EXTRAS=          仅 cli 模式：安装额外依赖组（例如 speedup）

说明：
  - 推荐保留 .git 仓库，这样 USE_GIT_ARCHIVE=1 能确保上下文里只有“原始项目文件”（不含未追踪文件）。
  - 如果当前目录不是 git 仓库，会自动退化为直接用当前目录做 build context，并依赖 .dockerignore 过滤。
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

mode="${1:-web}"
image="${2:-}"

default_tz="${TZ:-Asia/Shanghai}"
ref="${REF:-HEAD}"
use_git_archive="${USE_GIT_ARCHIVE:-1}"
no_cache="${NO_CACHE:-0}"
pip_extras="${PIP_EXTRAS:-}"

repo_root="$(pwd)"
if command -v git >/dev/null 2>&1; then
  if git_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    repo_root="$git_root"
  fi
fi

if [[ ! -f "${repo_root}/pyproject.toml" ]]; then
  echo "错误：未找到 ${repo_root}/pyproject.toml，请在项目根目录执行该脚本" >&2
  exit 2
fi

case "$mode" in
  web)
    dockerfile_rel="docker/Web.Dockerfile"
    image="${image:-tg-signer-web:latest}"
    ;;
  cli)
    dockerfile_rel="docker/Source.Dockerfile"
    image="${image:-tg-signer:latest}"
    ;;
  *)
    echo "错误：未知模式 '$mode'（仅支持 web|cli）" >&2
    usage
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未找到 docker，请先安装并确保 docker 命令可用" >&2
  exit 127
fi

dockerfile_path="${repo_root}/${dockerfile_rel}"
if [[ ! -f "$dockerfile_path" ]]; then
  echo "错误：未找到 Dockerfile: ${dockerfile_path}" >&2
  exit 2
fi

tmpdir=""
context_dir="$repo_root"

cleanup() {
  if [[ -n "${tmpdir}" && -d "${tmpdir}" ]]; then
    rm -rf "${tmpdir}"
  fi
}
trap cleanup EXIT

if [[ "${use_git_archive}" == "1" ]] && command -v git >/dev/null 2>&1; then
  if git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git -C "$repo_root" rev-parse "$ref" >/dev/null 2>&1; then
      tmpdir="$(mktemp -d)"
      git -C "$repo_root" archive --format=tar "$ref" | tar -x -C "$tmpdir"
      context_dir="$tmpdir"
    else
      echo "警告：REF=${ref} 不存在，已退化为直接用当前目录构建（依赖 .dockerignore）" >&2
    fi
  else
    echo "警告：当前不是 git 仓库，已退化为直接用当前目录构建（依赖 .dockerignore）" >&2
  fi
fi

build_args=(--build-arg "TZ=${default_tz}")
if [[ "$mode" == "cli" && -n "${pip_extras}" ]]; then
  build_args+=(--build-arg "PIP_EXTRAS=${pip_extras}")
fi

extra_flags=()
if [[ "${no_cache}" == "1" ]]; then
  extra_flags+=(--no-cache)
fi

echo "构建模式：${mode}"
echo "镜像标签：${image}"
echo "Dockerfile：${dockerfile_rel}"
echo "构建上下文：${context_dir}"

docker build \
  "${extra_flags[@]}" \
  "${build_args[@]}" \
  -f "${context_dir}/${dockerfile_rel}" \
  -t "${image}" \
  "${context_dir}"

echo "完成：${image}"

