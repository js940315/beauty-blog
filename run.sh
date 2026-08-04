#!/usr/bin/env bash
# 매일 실행 진입점.
#   ./run.sh --dry-run          제목까지만 (첫 실행 권장)
#   ./run.sh                    풀 생성
#   ./run.sh --celeb 김희선     인물 지정
#   ./run.sh --sort date        최신 기사 위주
set -euo pipefail

cd "$(dirname "$0")"

# .env 가 있으면 읽어들인다 (키를 셸 프로필에 안 넣어도 되게)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# 네이버 키는 없어도 된다 — 없으면 Google News RSS로 자동 폴백한다(근거가 얇아진다).
if [ -z "${NAVER_CLIENT_ID:-}" ] || [ -z "${NAVER_CLIENT_SECRET:-}" ]; then
  echo "[알림] 네이버 키가 없어 Google News RSS로 수집합니다."
fi

# ANTHROPIC_API_KEY 는 API 경로에서만 필요하다. --stage 경로는 에이전트가 글을 쓴다.
case " $* " in
  *" --stage "*) ;;
  *)
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "[중단] ANTHROPIC_API_KEY 가 없습니다."
      echo "       .env.example 을 .env 로 복사해 채우거나,"
      echo "       키 없이 가려면 에이전트 경로를 쓰세요: ./run.sh --stage crawl"
      exit 2
    fi
    ;;
esac

mkdir -p logs
export PYTHONIOENCODING=utf-8

PY=python
command -v python3 >/dev/null 2>&1 && PY=python3

exec "$PY" main.py "$@"
