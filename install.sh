#!/bin/bash
# convention-extractor v1.7 설치 확인 스크립트
# 폐쇄망 단일 스크립트 도구용 — 설치 시도 없이 환경만 확인

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== convention-extractor v1.7 install (2026-03-24) ==="
echo ""

# 1. Python3 확인
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "[ERROR] python3을 찾을 수 없습니다."
    echo "  설치: sudo dnf install python3  (또는 sudo apt install python3)"
    exit 1
fi

$PY --version
echo "[OK] Python found"
echo ""

# 2. 모듈 확인 (설치 시도 X, 안내만)
echo "Checking modules..."
$PY -c "import requests; import yaml; print('[OK] modules loaded')" 2>/dev/null || {
    echo "[ERROR] 필수 모듈이 없습니다."
    echo ""
    echo "  아래 명령으로 설치하세요:"
    echo "    pip3 install --user requests pyyaml"
    echo ""
    echo "  폐쇄망인 경우:"
    echo "    인터넷 PC에서 pip download requests pyyaml -d ./packages/"
    echo "    오프라인 PC에서 pip3 install --user --no-index --find-links=./packages/ requests pyyaml"
    exit 1
}

# 3. config.yaml 자동 생성
echo ""
if [ ! -f "$SCRIPT_DIR/config.yaml" ]; then
    if [ -f "$SCRIPT_DIR/config.example.yaml" ]; then
        cp "$SCRIPT_DIR/config.example.yaml" "$SCRIPT_DIR/config.yaml"
        echo "[OK] config.yaml created (from config.example.yaml)"
    else
        echo "[WARN] config.example.yaml not found — config.yaml 수동 생성 필요"
    fi
else
    echo "[OK] config.yaml ready"
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Usage:"
echo "  $PY extract_convention.py <project-path>"
echo "  $PY extract_convention.py <project-path> -o result"
