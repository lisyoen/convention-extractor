#!/usr/bin/env python3
"""
코딩 컨벤션 자동 추출 도구 v1.8
- 소스코드 디렉토리를 분석하여 언어별 컨벤션 파일 생성
- LLM API (OpenAI 호환) 사용
- C, Python, JavaScript/Vue 등 다중 언어 지원
- 설정 가능한 채택률 임계값 (기본 90%)
- 컨벤션 위반 파일 자동 탐지 (refactoring_needed_YYYYMMDD_hhmmss.txt)
  → 파일별 정리, 한국어 위반 설명, 개발자 친화적 포맷
- 통합 결과 로그 (extract_convention_result_YYYYMMDD_hhmmss.log)
  → 콘솔 로그 + 추출 컨벤션 + 정적 분석 통계 + 리팩토링 요약/상세
- convention.md에서 Statistics 섹션 제거 (통계는 결과 로그에만)
- Continue/Cline 시스템 프롬프트로 바로 사용 가능
- LLM 프롬프트 정확성 개선: 실제 코드 패턴 기반 판단 (업계 관행 적용 금지)
- 이상치(outlier) 파일 자동 탐지 및 LLM 컨벤션 추출에서 제외
- 기존 컨벤션 파일과 새 분석 결과를 자동 병합 (--merge)

설정 (우선순위: CLI 옵션 > 환경변수 > config.yaml > 기본값):
    1. config.yaml  - 스크립트와 같은 디렉토리에 배치
    2. 환경변수     - CONVENTION_API_BASE, CONVENTION_API_KEY, CONVENTION_MODEL, CONVENTION_ADOPTION_THRESHOLD
    3. CLI 옵션     - --api-base, --api-key, --model, --threshold

사용법:
    python extract_convention_v10_1.7.py /path/to/project
    python extract_convention_v10_1.7.py /path/to/project -o output_dir/
    python extract_convention_v10_1.7.py /path/to/project --lang python --max-files 1000
    python extract_convention_v10_1.7.py /path/to/project --threshold 80
    python extract_convention_v10_1.7.py /path/to/project --merge existing_convention.md
"""

import os
import sys
import json
import argparse
import re
import textwrap
import logging
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from collections import Counter, defaultdict
from datetime import datetime

import traceback

import requests

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


VERSION = "v1.8"
TOOL_NAME = f"코딩 컨벤션 자동 추출 도구 {VERSION}"
DEFAULT_ADOPTION_THRESHOLD = 90  # 컨벤션으로 인정하는 최소 채택률 (%)


# ============================================================
# 로깅 설정 (콘솔 + 파일 동시 출력)
# ============================================================

class TeeLogger:
    """콘솔과 파일에 동시 출력하는 로거"""
    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path
        self.log_file = None
        self.console_lines: List[str] = []
        if log_path:
            self.log_file = open(log_path, 'w', encoding='utf-8')
    
    def log(self, message: str):
        """콘솔과 파일에 동시 출력"""
        print(message)
        self.console_lines.append(message)
        if self.log_file:
            self.log_file.write(message + '\n')
            self.log_file.flush()
    
    def close(self):
        if self.log_file:
            self.log_file.close()

    def rewrite_log(self, full_content: str):
        """로그 파일을 전체 내용으로 다시 쓰기 (결과 요약 포함)"""
        if self.log_path:
            with open(self.log_path, 'w', encoding='utf-8') as f:
                f.write(full_content)


# 전역 로거 (나중에 초기화)
_logger: Optional[TeeLogger] = None


def log(message: str):
    """로그 출력 (콘솔 + 파일)"""
    global _logger
    if _logger:
        _logger.log(message)
    else:
        print(message)


# ============================================================
# 디버그 로깅 시스템 (v1.3: 문제 발생 시 파일에만 기록)
# ============================================================

class DebugLogger:
    """문제 발생 시에만 debug 로그 파일을 생성하여 기록"""
    
    def __init__(self, output_dir: str, timestamp: str):
        self.log_path = os.path.join(output_dir, f"debug_{timestamp}.log")
        self._file = None
        self._has_issues = False
    
    def _ensure_file(self):
        """첫 번째 문제 발생 시에만 파일 생성"""
        if self._file is None:
            self._file = open(self.log_path, 'w', encoding='utf-8')
            self._file.write(f"=== Debug Log (started: {datetime.now().isoformat()}) ===\n\n")
            self._has_issues = True
    
    def log(self, category: str, message: str, data: Optional[str] = None):
        """디버그 정보를 파일에 기록 (터미널에는 출력하지 않음)"""
        self._ensure_file()
        ts = datetime.now().strftime('%H:%M:%S')
        self._file.write(f"[{ts}] [{category}] {message}\n")
        if data:
            self._file.write(f"{data}\n")
        self._file.write("\n")
        self._file.flush()
    
    def close(self):
        if self._file:
            self._file.close()
    
    @property
    def has_issues(self) -> bool:
        return self._has_issues


# 전역 디버그 로거 (나중에 초기화)
_debug_logger: Optional[DebugLogger] = None


def debug_log(category: str, message: str, data: Optional[str] = None):
    """디버그 로그 기록 (파일에만, 문제 발생 시에만 파일 생성)"""
    global _debug_logger
    if _debug_logger:
        _debug_logger.log(category, message, data)


# ============================================================
# 설정 파일 로딩
# ============================================================

def _load_config_file():
    # type: () -> Dict
    """스크립트와 같은 디렉토리의 config.yaml (또는 config.json) 로드"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # YAML 우선
    yaml_path = os.path.join(script_dir, "config.yaml")
    if os.path.isfile(yaml_path):
        if HAS_YAML:
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                print(f"  ⚠️ config.yaml 로드 실패: {e}")
        else:
            print("  ⚠️ config.yaml 발견했으나 pyyaml 미설치. pip install pyyaml")

    # JSON 폴백
    json_path = os.path.join(script_dir, "config.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"  ⚠️ config.json 로드 실패: {e}")

    return {}


_FILE_CONFIG = _load_config_file()


def _cfg(key, env_key, fallback):
    """우선순위: 환경변수 > config 파일 > fallback"""
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    file_val = _FILE_CONFIG.get(key)
    if file_val is not None:
        return file_val
    return fallback


DEFAULT_API_BASE = _cfg("api_base", "CONVENTION_API_BASE", "http://localhost:11434/v1")
DEFAULT_API_KEY = _cfg("api_key", "CONVENTION_API_KEY", "no-key")
DEFAULT_MODEL = _cfg("model", "CONVENTION_MODEL", "qwen2.5-coder:32b")
DEFAULT_MAX_FILES = int(_cfg("max_files", "", "1000"))
DEFAULT_BATCH_SIZE = int(_cfg("batch_size", "", "5"))

# 채택률 임계값: 환경변수 > config 파일 > 기본값
_threshold_cfg = _cfg("adoption_threshold", "CONVENTION_ADOPTION_THRESHOLD", str(DEFAULT_ADOPTION_THRESHOLD))
DEFAULT_ADOPTION_THRESHOLD = int(_threshold_cfg)

# v1.4: 고급 설정 (config.yaml에서 읽기, 기본값 유지)
DEFAULT_MAX_TOKENS = int(_cfg("max_tokens", "", "4096"))
DEFAULT_TEMPERATURE = float(_cfg("temperature", "", "0.2"))
DEFAULT_MAX_FILE_LINES = int(_cfg("max_file_lines", "", "400"))
DEFAULT_MAX_FILE_SIZE = int(_cfg("max_file_size", "", "50000"))
DEFAULT_TIMEOUT = int(_cfg("timeout", "", "180"))
DEFAULT_VERBOSE = str(_cfg("verbose", "", "false")).lower() in ("true", "1", "yes")
DEFAULT_COMPLIANCE_BATCH_SIZE = int(_cfg("compliance_batch_size", "", "3"))

# v1.8: 언어 필터링 (config.yaml languages 섹션)
def _get_config_languages() -> Optional[set]:
    """config.yaml의 languages 섹션에서 true인 확장자 집합 반환.
    섹션이 없거나 비어있으면 None (전체 언어).
    """
    langs = _FILE_CONFIG.get("languages")
    if not langs or not isinstance(langs, dict):
        return None
    enabled = {ext for ext, val in langs.items() if val is True}
    return enabled if enabled else None

CONFIG_LANGUAGES = _get_config_languages()

# v1.8: 컨벤션 카테고리 선택 (config.yaml conventions 섹션)
def _get_config_conventions() -> Optional[set]:
    """config.yaml의 conventions 섹션에서 true인 카테고리 집합 반환.
    섹션이 없거나 비어있으면 None (전체 카테고리).
    """
    convs = _FILE_CONFIG.get("conventions")
    if not convs or not isinstance(convs, dict):
        return None
    enabled = {cat for cat, val in convs.items() if val is True}
    return enabled if enabled else None

CONFIG_CONVENTIONS = _get_config_conventions()

# 언어별 확장자 매핑
LANG_EXTENSIONS: Dict[str, set] = {
    "c": {".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".hxx"},
    "python": {".py", ".pyi"},
    "javascript": {".js", ".jsx", ".ts", ".tsx", ".vue", ".mjs", ".cjs"},
    "java": {".java"},
    "kotlin": {".kt", ".kts"},
    "go": {".go"},
    "rust": {".rs"},
}

# 확장자 → 언어 역매핑
EXT_TO_LANG: Dict[str, str] = {}
for lang, exts in LANG_EXTENSIONS.items():
    for ext in exts:
        EXT_TO_LANG[ext] = lang

ALL_EXTENSIONS = set()
for exts in LANG_EXTENSIONS.values():
    ALL_EXTENSIONS.update(exts)

# 제외 디렉토리
EXCLUDE_DIRS = {
    "node_modules", "target", "build", ".git", ".idea", ".vscode",
    "__pycache__", "dist", "out", ".gradle", "bin", ".next", ".nuxt",
    "vendor", ".tox", ".mypy_cache", ".pytest_cache", "egg-info",
    ".eggs", "venv", ".venv", "env", ".env", "coverage", ".coverage",
}

# 제외 파일 패턴
EXCLUDE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock",
}

MAX_FILE_LINES = DEFAULT_MAX_FILE_LINES  # 파일당 최대 읽기 라인 (v1.4: config에서 읽기)
MAX_FILE_SIZE = DEFAULT_MAX_FILE_SIZE  # 50KB (v1.4: config에서 읽기)
BATCH_SIZE = DEFAULT_BATCH_SIZE  # LLM 호출당 파일 수 (토큰 절약)


# ============================================================
# 정적 분석기 (LLM 호출 전 기본 통계 수집)
# ============================================================

class StaticAnalyzer:
    """LLM 없이 코드에서 기계적으로 추출 가능한 패턴 분석"""

    def __init__(self):
        self.stats = {
            "indent_tabs": 0,
            "indent_spaces": Counter(),  # space 수별 카운트
            "line_lengths": [],
            "semicolons": Counter(),  # True/False
            "quote_single": 0,
            "quote_double": 0,
            "brace_same_line": 0,  # K&R: {는 같은 줄
            "brace_next_line": 0,  # Allman: {는 다음 줄
            "naming_snake": 0,
            "naming_camel": 0,
            "naming_pascal": 0,
            "naming_upper_snake": 0,
            "has_type_hints": 0,
            "no_type_hints": 0,
            "pointer_star_left": 0,   # int* ptr
            "pointer_star_right": 0,  # int *ptr
            "header_guard_ifndef": 0,
            "header_guard_pragma": 0,
            "files_by_lang": Counter(),
            "total_lines": 0,
            "total_files": 0,
            "var_count": 0,
            "const_count": 0,
            "let_count": 0,
        }
        # v1.6: 파일별 스타일 프로파일 (이상치 탐지용)
        self.file_profiles: Dict[str, dict] = {}

    def analyze_file(self, path: Path, content: str, lang: str):
        """파일 하나에 대한 정적 분석"""
        lines = content.split("\n")
        self.stats["total_files"] += 1
        self.stats["total_lines"] += len(lines)
        self.stats["files_by_lang"][lang] += 1

        # v1.6: 파일별 프로파일 수집용 변수
        file_indent_tabs = 0
        file_indent_spaces = Counter()

        for line in lines:
            stripped = line.lstrip()
            if not stripped:
                continue

            # 라인 길이
            self.stats["line_lengths"].append(len(line))

            # 들여쓰기 분석
            if line != stripped:
                indent = line[: len(line) - len(stripped)]
                if "\t" in indent:
                    self.stats["indent_tabs"] += 1
                    file_indent_tabs += 1
                else:
                    spaces = len(indent)
                    if spaces > 0:
                        self.stats["indent_spaces"][spaces] += 1
                        file_indent_spaces[spaces] += 1

        # 언어별 추가 분석
        if lang == "c":
            self._analyze_c(content)
        elif lang == "python":
            self._analyze_python(content)
        elif lang == "javascript":
            self._analyze_js(content)

        # 공통: 네이밍 패턴 (함수/변수 정의 추출)
        self._analyze_naming(content, lang)

        # v1.6: 파일별 스타일 프로파일 기록
        file_key = str(path)
        profile = {"lang": lang}

        # 들여쓰기 프로파일
        if file_indent_tabs > sum(file_indent_spaces.values()):
            profile["indent"] = "tabs"
        elif file_indent_spaces:
            from math import gcd
            from functools import reduce
            most_common = file_indent_spaces.most_common(3)
            sizes = [k for k, v in most_common]
            unit = reduce(gcd, sizes) if sizes else 4
            if unit in (2, 4, 8):
                profile["indent"] = f"{unit}spaces"
            else:
                profile["indent"] = "4spaces"
        else:
            profile["indent"] = "unknown"

        # 네이밍 프로파일 (파일 단위)
        if lang == "python":
            names = re.findall(r"def\s+(\w+)", content)
        elif lang == "c":
            names = re.findall(r"(?:void|int|char|long|static|unsigned|bool|size_t)\s+(\w+)\s*\(", content)
        elif lang == "javascript":
            names = re.findall(r"(?:function|const|let|var)\s+(\w+)", content)
        else:
            names = re.findall(r"(?:def|function|func|fn)\s+(\w+)", content)

        file_snake = 0
        file_camel = 0
        for name in names:
            n = name.lstrip("_")
            if not n:
                continue
            if "_" in n and n.islower():
                file_snake += 1
            elif n[0].islower() and any(c.isupper() for c in n):
                file_camel += 1
        if file_snake > file_camel:
            profile["naming"] = "snake_case"
        elif file_camel > file_snake:
            profile["naming"] = "camelCase"
        else:
            profile["naming"] = "mixed"

        # JS 특화: var vs const/let, 세미콜론, JSDoc
        if lang == "javascript":
            var_count = len(re.findall(r'\bvar\b', content))
            const_let_count = len(re.findall(r'\b(?:const|let)\b', content))
            profile["var_usage"] = "var" if var_count > const_let_count else "const/let"

            lines_with_semi = len(re.findall(r";\s*$", content, re.MULTILINE))
            lines_code = len(re.findall(r"[^;{}\s/]\s*$", content, re.MULTILINE))
            total_endings = lines_with_semi + lines_code
            profile["semicolon"] = "yes" if (total_endings > 0 and lines_with_semi / max(total_endings, 1) > 0.5) else "no"

            profile["jsdoc"] = "yes" if re.search(r'/\*\*', content) else "no"
        elif lang == "python":
            profile["var_usage"] = None
            profile["semicolon"] = None
            profile["jsdoc"] = "yes" if re.search(r'"""[\s\S]*?"""', content) or re.search(r"'''[\s\S]*?'''", content) else "no"
        elif lang == "c":
            profile["var_usage"] = None
            profile["semicolon"] = None
            profile["jsdoc"] = "yes" if re.search(r'/\*\*', content) or re.search(r'/\*[^*]', content) else "no"
        else:
            profile["var_usage"] = None
            profile["semicolon"] = None
            profile["jsdoc"] = "yes" if re.search(r'/\*\*', content) else "no"

        self.file_profiles[file_key] = profile

    def _analyze_c(self, content: str):
        # 포인터 스타일
        self.stats["pointer_star_left"] += len(re.findall(r"\w+\*\s+\w+", content))
        self.stats["pointer_star_right"] += len(re.findall(r"\w+\s+\*\w+", content))

        # 헤더 가드
        if "#pragma once" in content:
            self.stats["header_guard_pragma"] += 1
        if re.search(r"#ifndef\s+\w+_H", content):
            self.stats["header_guard_ifndef"] += 1

        # 브레이스 스타일
        self.stats["brace_same_line"] += len(
            re.findall(r"(?:if|for|while|else|switch|struct|enum|union)\s*\(.*\)\s*\{", content)
        )
        self.stats["brace_same_line"] += len(
            re.findall(r"(?:void|int|char|long|static|unsigned)\s+\w+\s*\(.*\)\s*\{", content)
        )
        self.stats["brace_next_line"] += len(
            re.findall(r"(?:if|for|while|else|switch)\s*\(.*\)\s*\n\s*\{", content)
        )

    def _analyze_python(self, content: str):
        # 타입 힌트
        if re.search(r"def\s+\w+\(.*:\s*\w+", content) or re.search(r"->\s*\w+", content):
            self.stats["has_type_hints"] += 1
        else:
            self.stats["no_type_hints"] += 1

    def _analyze_js(self, content: str):
        # 세미콜론
        lines_with_semi = len(re.findall(r";\s*$", content, re.MULTILINE))
        lines_without = len(re.findall(r"[^;{}\s/]\s*$", content, re.MULTILINE))
        if lines_with_semi > lines_without:
            self.stats["semicolons"]["yes"] += 1
        else:
            self.stats["semicolons"]["no"] += 1

        # var / const / let 카운트
        self.stats["var_count"] += len(re.findall(r'\bvar\b', content))
        self.stats["const_count"] += len(re.findall(r'\bconst\b', content))
        self.stats["let_count"] += len(re.findall(r'\blet\b', content))

        # 따옴표
        self.stats["quote_single"] += len(re.findall(r"'[^']*'", content))
        self.stats["quote_double"] += len(re.findall(r'"[^"]*"', content))

        # 브레이스
        self.stats["brace_same_line"] += len(
            re.findall(r"(?:function|if|for|while|else|class|=>)\s*.*\{", content)
        )

    def _analyze_naming(self, content: str, lang: str):
        # 함수명 추출
        if lang == "python":
            names = re.findall(r"def\s+(\w+)", content)
        elif lang == "c":
            names = re.findall(r"(?:void|int|char|long|static|unsigned|bool|size_t)\s+(\w+)\s*\(", content)
        elif lang == "javascript":
            names = re.findall(r"(?:function|const|let|var)\s+(\w+)", content)
        else:
            names = re.findall(r"(?:def|function|func|fn)\s+(\w+)", content)

        for name in names:
            if name.startswith("_"):
                name = name.lstrip("_")
            if not name:
                continue
            if name.isupper() and "_" in name:
                self.stats["naming_upper_snake"] += 1
            elif "_" in name and name.islower():
                self.stats["naming_snake"] += 1
            elif name[0].isupper() and "_" not in name:
                self.stats["naming_pascal"] += 1
            elif name[0].islower() and any(c.isupper() for c in name):
                self.stats["naming_camel"] += 1

    def get_summary(self) -> Dict:
        """정적 분석 요약 (LLM 프롬프트에 포함)"""
        summary = {}

        # 들여쓰기 - 비율 포함
        total_indented = sum(self.stats["indent_spaces"].values()) + self.stats["indent_tabs"]
        if self.stats["indent_tabs"] > sum(self.stats["indent_spaces"].values()):
            if total_indented > 0:
                pct = round(self.stats["indent_tabs"] / total_indented * 100)
                summary["indentation"] = f"tabs ({pct}% of indented lines)"
            else:
                summary["indentation"] = "tabs"
        else:
            if self.stats["indent_spaces"]:
                # 2칸과 4칸 각각의 비율 계산
                two_space = self.stats["indent_spaces"].get(2, 0) + self.stats["indent_spaces"].get(6, 0)
                four_space = self.stats["indent_spaces"].get(4, 0) + self.stats["indent_spaces"].get(8, 0) + self.stats["indent_spaces"].get(12, 0)
                if total_indented > 0:
                    if two_space > four_space:
                        pct = round(two_space / total_indented * 100)
                        summary["indentation"] = f"2 spaces ({pct}% of indented lines)"
                    elif four_space > two_space:
                        pct = round(four_space / total_indented * 100)
                        summary["indentation"] = f"4 spaces ({pct}% of indented lines)"
                    else:
                        # 동률이면 gcd 방식 폴백
                        most_common = self.stats["indent_spaces"].most_common(3)
                        if most_common:
                            from math import gcd
                            from functools import reduce
                            sizes = [k for k, v in most_common]
                            unit = reduce(gcd, sizes)
                            if unit in (2, 4, 8):
                                pct = round(sum(v for k, v in self.stats["indent_spaces"].items() if k % unit == 0) / total_indented * 100)
                                summary["indentation"] = f"{unit} spaces ({pct}% of indented lines)"
                            else:
                                summary["indentation"] = "4 spaces (추정)"
                else:
                    summary["indentation"] = "4 spaces (추정)"

        # 라인 길이
        if self.stats["line_lengths"]:
            lengths = sorted(self.stats["line_lengths"])
            p90 = lengths[int(len(lengths) * 0.9)] if lengths else 80
            summary["line_length_p90"] = p90

        # 네이밍 - 비율 포함
        naming = {
            "snake_case": self.stats["naming_snake"],
            "camelCase": self.stats["naming_camel"],
            "PascalCase": self.stats["naming_pascal"],
            "UPPER_SNAKE": self.stats["naming_upper_snake"],
        }
        total_names = sum(naming.values())
        if total_names > 0:
            summary["naming_stats"] = {
                k: f"{v} ({round(v / total_names * 100)}%)"
                for k, v in naming.items() if v > 0
            }
        else:
            summary["naming_stats"] = {}

        # JS 특화
        if self.stats["semicolons"]:
            total_semi = sum(self.stats["semicolons"].values())
            most_common_semi, most_common_count = self.stats["semicolons"].most_common(1)[0]
            if total_semi > 0:
                pct = round(most_common_count / total_semi * 100)
                summary["semicolons"] = f"{most_common_semi} ({pct}% of files)"
            else:
                summary["semicolons"] = most_common_semi
        if self.stats["quote_single"] + self.stats["quote_double"] > 0:
            summary["quotes"] = (
                "single" if self.stats["quote_single"] > self.stats["quote_double"]
                else "double"
            )

        # JS 변수 선언: var vs const/let 비율
        total_var_decl = self.stats["var_count"] + self.stats["const_count"] + self.stats["let_count"]
        if total_var_decl > 0:
            var_pct = round(self.stats["var_count"] / total_var_decl * 100)
            const_pct = round(self.stats["const_count"] / total_var_decl * 100)
            let_pct = round(self.stats["let_count"] / total_var_decl * 100)
            summary["variable_declarations"] = {
                "var": f"{self.stats['var_count']} ({var_pct}%)",
                "const": f"{self.stats['const_count']} ({const_pct}%)",
                "let": f"{self.stats['let_count']} ({let_pct}%)",
            }

        # C 특화
        if self.stats["brace_same_line"] + self.stats["brace_next_line"] > 0:
            summary["brace_style"] = (
                "K&R (same line)" if self.stats["brace_same_line"] > self.stats["brace_next_line"]
                else "Allman (next line)"
            )
        if self.stats["pointer_star_left"] + self.stats["pointer_star_right"] > 0:
            summary["pointer_style"] = (
                "int* ptr" if self.stats["pointer_star_left"] > self.stats["pointer_star_right"]
                else "int *ptr"
            )
        if self.stats["header_guard_ifndef"] + self.stats["header_guard_pragma"] > 0:
            summary["header_guard"] = (
                "#pragma once" if self.stats["header_guard_pragma"] > self.stats["header_guard_ifndef"]
                else "#ifndef GUARD"
            )

        # Python 특화
        if self.stats["has_type_hints"] + self.stats["no_type_hints"] > 0:
            summary["type_hints"] = (
                "사용" if self.stats["has_type_hints"] > self.stats["no_type_hints"]
                else "미사용"
            )

        # 파일 통계
        summary["total_files"] = self.stats["total_files"]
        summary["total_lines"] = self.stats["total_lines"]
        summary["files_by_lang"] = dict(self.stats["files_by_lang"])

        return summary

    def detect_outliers(self, file_stats: List[Tuple[Path, str]]) -> List[Tuple[str, int, int]]:
        """
        정적분석 결과를 기반으로 다수 파일과 스타일이 크게 다른 이상치 파일을 탐지.
        
        각 파일에 대해 아래 항목을 다수결과 비교:
        1. 들여쓰기 (2칸 vs 4칸 vs 탭)
        2. 변수 선언 (var vs const/let)  
        3. 세미콜론 사용 여부
        4. 네이밍 패턴 (camelCase vs snake_case)
        5. JSDoc/주석 존재 여부
        
        3개 이상 항목에서 다수와 다르면 이상치로 판정.
        같은 언어 파일끼리만 비교. 파일이 3개 미만이면 스킵.
        
        Args:
            file_stats: (path, lang) 튜플 리스트
        
        Returns:
            이상치로 판정된 (파일 경로 문자열, 불일치 수, 비교 항목 수) 리스트
        """
        outliers = []

        # 언어별로 그룹핑
        lang_groups: Dict[str, List[str]] = defaultdict(list)
        for path, lang in file_stats:
            key = str(path)
            if key in self.file_profiles:
                lang_groups[lang].append(key)

        for lang, file_keys in lang_groups.items():
            # 3개 미만이면 이상치 탐지 스킵
            if len(file_keys) < 3:
                continue

            profiles = [(k, self.file_profiles[k]) for k in file_keys]

            # 각 비교 항목에 대해 다수결 계산
            comparisons = []  # (항목명, 추출함수)

            # 1. 들여쓰기
            indent_counter = Counter(p["indent"] for _, p in profiles if p.get("indent") and p["indent"] != "unknown")
            if indent_counter:
                majority_indent = indent_counter.most_common(1)[0][0]
                comparisons.append(("indent", majority_indent))

            # 2. 네이밍 패턴
            naming_counter = Counter(p["naming"] for _, p in profiles if p.get("naming") and p["naming"] != "mixed")
            if naming_counter:
                majority_naming = naming_counter.most_common(1)[0][0]
                comparisons.append(("naming", majority_naming))

            # 3. 변수 선언 (var vs const/let) — JS만
            var_counter = Counter(p["var_usage"] for _, p in profiles if p.get("var_usage"))
            if var_counter:
                majority_var = var_counter.most_common(1)[0][0]
                comparisons.append(("var_usage", majority_var))

            # 4. 세미콜론 — JS만
            semi_counter = Counter(p["semicolon"] for _, p in profiles if p.get("semicolon"))
            if semi_counter:
                majority_semi = semi_counter.most_common(1)[0][0]
                comparisons.append(("semicolon", majority_semi))

            # 5. JSDoc/주석 존재 여부
            jsdoc_counter = Counter(p["jsdoc"] for _, p in profiles if p.get("jsdoc"))
            if jsdoc_counter:
                majority_jsdoc = jsdoc_counter.most_common(1)[0][0]
                comparisons.append(("jsdoc", majority_jsdoc))

            if not comparisons:
                continue

            # 각 파일을 다수결과 비교
            for file_key, profile in profiles:
                mismatches = 0
                total_compared = len(comparisons)
                for attr_name, majority_val in comparisons:
                    file_val = profile.get(attr_name)
                    if file_val is None:
                        continue
                    if attr_name == "naming" and file_val == "mixed":
                        continue  # mixed는 비교 제외
                    if file_val != majority_val:
                        mismatches += 1

                # 3개 이상 항목에서 다수와 다르면 이상치
                if mismatches >= 3:
                    outliers.append((file_key, mismatches, total_compared))

        return outliers


# ============================================================
# 파일 수집
# ============================================================

def find_source_files(
    project_path: str,
    lang_filter: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    """소스 파일 수집, (경로, 언어) 튜플 리스트 반환"""
    project = Path(project_path).resolve()
    if not project.is_dir():
        log(f"❌ 디렉토리가 존재하지 않습니다: {project}")
        sys.exit(1)

    # 필터링할 확장자 결정
    if lang_filter:
        allowed_exts = LANG_EXTENSIONS.get(lang_filter)
        if not allowed_exts:
            log(f"❌ 지원하지 않는 언어: {lang_filter}")
            log(f"   지원 언어: {', '.join(LANG_EXTENSIONS.keys())}")
            sys.exit(1)
    else:
        allowed_exts = ALL_EXTENSIONS

    files = []
    for file_path in project.rglob("*"):
        if file_path.is_dir():
            continue
        # 제외 디렉토리
        if any(part in EXCLUDE_DIRS for part in file_path.parts):
            continue
        # 제외 파일
        if file_path.name in EXCLUDE_FILES:
            continue
        # 확장자 확인
        ext = file_path.suffix.lower()
        if ext not in allowed_exts:
            continue
        # 파일 크기 확인
        try:
            size = file_path.stat().st_size
            if size > MAX_FILE_SIZE or size < 10:
                continue
        except OSError:
            continue

        lang = EXT_TO_LANG.get(ext, "unknown")
        files.append((file_path, lang))

    # 파일 크기순 정렬 (작은 것부터 — 짧은 파일이 컨벤션 파악에 더 명확)
    files.sort(key=lambda x: x[0].stat().st_size)
    return files


def find_all_source_files(
    project_path: str,
    lang_filter: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    """전체 소스 파일 수집 (max_files 제한 없이, compliance check 용)"""
    project = Path(project_path).resolve()
    if not project.is_dir():
        return []

    if lang_filter:
        allowed_exts = LANG_EXTENSIONS.get(lang_filter, ALL_EXTENSIONS)
    else:
        allowed_exts = ALL_EXTENSIONS

    files = []
    for file_path in project.rglob("*"):
        if file_path.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in file_path.parts):
            continue
        if file_path.name in EXCLUDE_FILES:
            continue
        ext = file_path.suffix.lower()
        if ext not in allowed_exts:
            continue
        try:
            size = file_path.stat().st_size
            if size > MAX_FILE_SIZE or size < 10:
                continue
        except OSError:
            continue
        lang = EXT_TO_LANG.get(ext, "unknown")
        files.append((file_path, lang))

    return files


def filter_files_by_config_languages(
    files: List[Tuple[Path, str]],
    config_languages: Optional[set],
) -> List[Tuple[Path, str]]:
    """v1.8: config.yaml의 languages 섹션으로 파일 필터링.
    
    config_languages가 None이면 필터링 없이 전체 반환.
    확장자 기준 매칭: config에 'py'가 있으면 .py, .pyi 모두 포함.
    """
    if config_languages is None:
        return files
    
    # config에 지정된 확장자를 실제 확장자로 변환
    allowed_exts = set()
    for ext_key in config_languages:
        # 직접 매칭: .{ext_key}
        allowed_exts.add(f".{ext_key}")
        # LANG_EXTENSIONS에서 매칭 (예: 'c' → {'.c', '.h', '.cpp', ...})
        if ext_key in LANG_EXTENSIONS:
            allowed_exts.update(LANG_EXTENSIONS[ext_key])
    
    filtered = [(fp, lang) for fp, lang in files if fp.suffix.lower() in allowed_exts]
    return filtered


def read_file_safe(file_path: Path) -> Optional[str]:
    """파일 안전하게 읽기"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[:MAX_FILE_LINES]
            return "".join(lines)
    except Exception as e:
        log(f"  ⚠️ 읽기 실패: {e}")
        debug_log("FILE_READ_ERROR", f"파일 읽기 실패: {file_path}", f"에러: {e}\n{traceback.format_exc()}")
        return None


# ============================================================
# LLM 프롬프트 & 호출
# ============================================================

# v1.8: 카테고리별 프롬프트 블록 (동적 조립용)
_CATEGORY_BLOCKS = {
    "naming": """### 1. Naming Conventions
- Variables, functions, methods, classes, constants, interfaces
- File naming patterns
- Prefix/suffix conventions (e.g., I for interfaces, _ for private)""",
    "formatting": """### 2. Formatting
- Indentation (tabs/spaces, size)
- Brace style (K&R, Allman, etc.)
- Max line length
- Trailing commas
- Semicolons (JS/TS)
- Quote style (single/double)""",
    "code_organization": """### 3. Code Organization
- Import ordering and grouping
- File structure patterns
- Module/package organization""",
    "comments": """### 4. Comments & Documentation
- Comment style (single-line, multi-line)
- Docstring format (Google, NumPy, Sphinx, JSDoc, Doxygen)
- File headers (license, author, description)
- TODO/FIXME conventions""",
    "patterns": """### 5. Language-Specific Patterns
- Error handling patterns (try/catch, goto cleanup, Result type)
- Logging patterns (custom macros, logging library usage)
- Type annotations / type hints
- Pointer style (C/C++)
- Header guards (C/C++)
- Async patterns (JS/Python)
- Component patterns (Vue/React)""",
    "project_patterns": """### 6. Project Patterns
- Design patterns observed (Factory, Singleton, etc.)
- Dependency injection style
- API/HTTP client patterns
- State management patterns""",
}

# v1.8: 카테고리별 JSON 출력 포맷 블록
_CATEGORY_JSON_BLOCKS = {
    "naming": """    "naming": {{
      "variables": {{"description": "description", "adoption_rate": 95}},
      "functions": {{"description": "description", "adoption_rate": 90}},
      "classes": {{"description": "description", "adoption_rate": 100}},
      "constants": {{"description": "description", "adoption_rate": 85}},
      "files": {{"description": "description", "adoption_rate": 92}}
    }}""",
    "formatting": """    "formatting": {{
      "indentation": {{"description": "description", "adoption_rate": 98}},
      "brace_style": {{"description": "description", "adoption_rate": 95}},
      "max_line_length": {{"description": "description", "adoption_rate": 80}},
      "semicolons": {{"description": "description (JS only)", "adoption_rate": 90}},
      "quotes": {{"description": "description (JS only)", "adoption_rate": 88}},
      "trailing_commas": {{"description": "description", "adoption_rate": 70}}
    }}""",
    "code_organization": """    "code_organization": {{
      "imports": {{"description": "description", "adoption_rate": 85}},
      "file_structure": {{"description": "description", "adoption_rate": 90}}
    }}""",
    "comments": """    "comments": {{
      "single_line": {{"description": "description", "adoption_rate": 95}},
      "multi_line": {{"description": "description", "adoption_rate": 80}},
      "docstring": {{"description": "description", "adoption_rate": 60}},
      "file_header": {{"description": "description", "adoption_rate": 50}}
    }}""",
    "patterns": """    "patterns": {{
      "error_handling": {{"description": "description", "adoption_rate": 90}},
      "logging": {{"description": "description", "adoption_rate": 85}},
      "type_annotations": {{"description": "description", "adoption_rate": 70}},
      "other": [{{"description": "pattern1", "adoption_rate": 80}}, {{"description": "pattern2", "adoption_rate": 75}}]
    }}""",
    "project_patterns": """    "project_patterns": {{
      "design_patterns": {{"description": "description", "adoption_rate": 80}},
      "dependency_injection": {{"description": "description", "adoption_rate": 70}},
      "api_patterns": {{"description": "description", "adoption_rate": 85}}
    }}""",
}

# 전체 카테고리 순서
_ALL_CATEGORIES = ["naming", "formatting", "code_organization", "comments", "patterns", "project_patterns"]


def build_analysis_prompt(
    static_summary: str,
    file_blocks: str,
    selected_conventions: Optional[set] = None,
) -> str:
    """v1.8: ANALYSIS_PROMPT를 동적으로 조립.
    
    Args:
        static_summary: 정적 분석 결과 JSON 문자열
        file_blocks: 파일 블록 문자열
        selected_conventions: 선택된 카테고리 집합 (None이면 전체)
    """
    # 카테고리 결정
    if selected_conventions:
        categories = [c for c in _ALL_CATEGORIES if c in selected_conventions]
    else:
        categories = _ALL_CATEGORIES

    # 가이드라인 블록 조립
    guidelines = "\n\n".join(_CATEGORY_BLOCKS[c] for c in categories if c in _CATEGORY_BLOCKS)

    # JSON 출력 포맷 블록 조립
    json_blocks = ",\n".join(_CATEGORY_JSON_BLOCKS[c] for c in categories if c in _CATEGORY_JSON_BLOCKS)

    # 카테고리 필터링 안내 (일부만 선택된 경우)
    category_note = ""
    if selected_conventions and len(categories) < len(_ALL_CATEGORIES):
        excluded = [c for c in _ALL_CATEGORIES if c not in categories]
        category_note = f"\n\n**NOTE**: Only analyze the categories listed above. Do NOT include these categories: {', '.join(excluded)}."

    prompt = f"""You are a senior software engineer analyzing source code to extract coding conventions.

## CRITICAL INSTRUCTION — Observe, Don't Prescribe
**IMPORTANT: Base ALL convention judgments ONLY on patterns actually observed in the provided source code. Do NOT apply general industry best practices or common conventions. If 90% of files use Korean comments, then Korean comments IS the convention. Only report what you actually see in the code.**

각 컨벤션 판단은 반드시 제공된 소스코드에서 실제로 관찰된 패턴에만 기반해야 합니다. 일반적인 업계 관행이나 교과서적 표준을 적용하지 마세요.

For example:
- If most files use Korean docstrings → report "Korean docstrings", NOT "English docstrings"
- If most files have no file headers → report "no file headers", NOT "license headers recommended"
- If most files use 2-space indentation → report "2 spaces", NOT "4 spaces (PEP8)"

## Task
Analyze the following source files and extract ALL coding conventions, patterns, and style rules used in this project.
For EACH convention item, estimate the **adoption_rate** (0-100): the percentage of analyzed files that follow this convention.

## Static Analysis Results (pre-computed, MUST FOLLOW)
The following statistics are computed from ALL files. Use these as the ground truth for formatting conventions.
If static analysis says "2 spaces (91%)", then the convention IS 2-space indentation, even if some files use 4 spaces.
```json
{static_summary}
```

## Source Files
{file_blocks}

## Analysis Guidelines
Focus on these categories:

{guidelines}{category_note}

## Output Format
Output ONLY a valid JSON object with this structure (no markdown fences, no extra text):
{{{{
  "language": "primary language",
  "conventions": {{{{
{json_blocks}
  }}}},
  "examples": {{{{
    "good": ["example of correct style"],
    "avoid": ["example of what to avoid"]
  }}}},
  "confidence": "high/medium/low"
}}}}

IMPORTANT:
- For every convention item, include "adoption_rate" as a number 0-100 representing the estimated percentage of files that follow this convention.
- Only include fields where you found evidence. Skip fields with no data.
- adoption_rate should reflect how consistently this pattern appears across the analyzed files.
- REMEMBER: Report ONLY what you observe in the actual code. Do NOT inject best practices, PEP8 rules, or industry standards that aren't reflected in the code. The goal is to describe THIS project's actual style, not an ideal style.
"""
    return prompt

MERGE_PROMPT = """You are merging coding convention analyses from multiple batches into one final result.

## CRITICAL INSTRUCTION — Observe, Don't Prescribe
**IMPORTANT: Base ALL convention judgments ONLY on patterns actually observed in the provided source code. Do NOT apply general industry best practices or common conventions. If 90% of files use Korean comments, then Korean comments IS the convention. Only report what you actually see in the code.**

각 컨벤션 판단은 반드시 제공된 소스코드에서 실제로 관찰된 패턴에만 기반해야 합니다. 일반적인 업계 관행이나 교과서적 표준을 적용하지 마세요.

## Previous Analysis
```json
{previous}
```

## New Analysis (from additional files)
```json
{new_analysis}
```

## Additional Source Files Context
{file_blocks}

## Instructions
1. Merge the two analyses into one comprehensive result
2. If conventions conflict, prefer the pattern with more evidence
3. Add any new patterns found in the additional files
4. Update confidence based on consistency across all files
5. Keep the same JSON output format
6. **Update adoption_rate for each convention**: recalculate based on combined evidence from all batches. adoption_rate should be 0-100 representing the percentage of files following this convention.
7. **REMEMBER**: Report ONLY what you observe in the actual code. Do NOT inject best practices or industry standards. Describe THIS project's real style.

Output ONLY the merged JSON object (no markdown fences, no extra text).
"""

COMPLIANCE_CHECK_PROMPT = """You are checking if source files follow the project's coding conventions.

## Project Conventions
```json
{conventions}
```

## File to Check: {file_path}
```{ext}
{file_content}
```

## Task
Check this file against each convention that has adoption_rate >= {threshold}%.
List ONLY the conventions that this file VIOLATES.
**Only report violations. Do NOT report items that are already compliant.**

Output ONLY a valid JSON object (no markdown fences):
{{
  "violations": [
    {{"convention": "convention name", "detail": "what's wrong", "expected": "what's expected"}}
  ]
}}

If the file follows all conventions, output:
{{"violations": []}}
"""


def call_llm(
    prompt: str,
    api_base: str,
    api_key: str,
    model: str,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """OpenAI 호환 API 호출"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    url = f"{api_base.rstrip('/')}/chat/completions"
    prompt_length = len(data["messages"][0]["content"]) if data["messages"] else 0

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=timeout,
            verify=False,
        )
        resp.raise_for_status()
        result = resp.json()
        return result["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        log(f"  ⚠️ API 타임아웃 ({timeout}s)")
        debug_log("API_TIMEOUT", f"API 타임아웃 ({timeout}s)", f"URL: {url}\n모델: {model}\n프롬프트 길이: {prompt_length}")
        return None
    except requests.exceptions.ConnectionError:
        log(f"  ⚠️ API 연결 실패: {api_base}")
        debug_log("API_CONNECTION_ERROR", f"API 연결 실패", f"URL: {url}\n모델: {model}")
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")[:200]
        except Exception:
            pass
        log(f"  ⚠️ API HTTP {status}: {detail or str(e)[:200]}")
        debug_log("API_HTTP_ERROR", f"HTTP {status}", f"URL: {url}\n모델: {model}\n상태코드: {status}\n상세: {detail or str(e)[:500]}")
        return None
    except Exception as e:
        log(f"  ⚠️ API 호출 실패: {e}")
        debug_log("API_ERROR", f"API 호출 실패: {e}", f"URL: {url}\n모델: {model}\n{traceback.format_exc()}")
        return None


def parse_json_response(text: str, batch_info: str = "", model: str = "", prompt_length: int = 0) -> Optional[Dict]:
    """LLM 응답에서 JSON 추출 (v1.2: thinking 블록 제거, v1.3: 실패 시 debug 로깅)"""
    text = text.strip()
    # v1.2: Qwen3 등 thinking 모드 모델의 <think>...</think> 블록 제거
    text = re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
    # v1.4.1: max_tokens 부족으로 </think> 닫히지 않은 경우도 제거
    text = re.sub(r'<think>[\s\S]*$', '', text).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # v1.3: JSON 파싱 실패 시 디버그 로그에 raw 응답 전체 기록
    debug_log(
        "JSON_PARSE_ERROR",
        f"JSON 파싱 실패 | 배치: {batch_info} | 모델: {model} | 프롬프트 길이: {prompt_length}",
        f"--- RAW 응답 전체 ---\n{text}\n--- END ---"
    )
    return None


# ============================================================
# 채택률 기반 컨벤션 분류
# ============================================================

def classify_conventions(analysis: Dict, threshold: int) -> Tuple[Dict, Dict]:
    """컨벤션을 채택률 기준으로 '확정'과 '미달' 로 분류.

    Returns:
        (accepted, rejected) — 각각 같은 구조의 dict
        accepted: adoption_rate >= threshold
        rejected: adoption_rate < threshold
    """
    accepted = {}
    rejected = {}
    conventions = analysis.get("conventions", {})

    for category, items in conventions.items():
        if not isinstance(items, dict):
            continue
        accepted_cat = {}
        rejected_cat = {}
        for key, val in items.items():
            if isinstance(val, dict) and "adoption_rate" in val:
                rate = val.get("adoption_rate", 0)
                if isinstance(rate, (int, float)) and rate >= threshold:
                    accepted_cat[key] = val
                else:
                    rejected_cat[key] = val
            elif isinstance(val, list):
                # patterns.other 같은 리스트
                acc_list = []
                rej_list = []
                for item in val:
                    if isinstance(item, dict) and "adoption_rate" in item:
                        rate = item.get("adoption_rate", 0)
                        if isinstance(rate, (int, float)) and rate >= threshold:
                            acc_list.append(item)
                        else:
                            rej_list.append(item)
                    else:
                        # adoption_rate 없는 항목은 그대로 accepted 처리
                        acc_list.append(item)
                if acc_list:
                    accepted_cat[key] = acc_list
                if rej_list:
                    rejected_cat[key] = rej_list
            else:
                # 구 형식 (plain string) — v3 호환, accepted로 취급
                accepted_cat[key] = val

        if accepted_cat:
            accepted[category] = accepted_cat
        if rejected_cat:
            rejected[category] = rejected_cat

    return accepted, rejected


def _get_convention_description(val) -> str:
    """컨벤션 항목에서 description 문자열 추출"""
    if isinstance(val, dict):
        return val.get("description", str(val))
    return str(val)


def _get_adoption_rate(val) -> Optional[int]:
    """컨벤션 항목에서 채택률 추출"""
    if isinstance(val, dict):
        rate = val.get("adoption_rate")
        if isinstance(rate, (int, float)):
            return int(rate)
    return None


# ============================================================
# 카테고리명 한국어 매핑
# ============================================================

CATEGORY_KO = {
    "naming": "네이밍",
    "formatting": "포매팅",
    "code_organization": "코드 구조",
    "comments": "주석/문서화",
    "patterns": "패턴",
}

CONVENTION_KEY_KO = {
    "variables": "변수",
    "functions": "함수",
    "methods": "메서드",
    "classes": "클래스",
    "constants": "상수",
    "files": "파일명",
    "interfaces": "인터페이스",
    "indentation": "들여쓰기",
    "brace_style": "브레이스 스타일",
    "max_line_length": "최대 줄 길이",
    "semicolons": "세미콜론",
    "quotes": "따옴표",
    "trailing_commas": "트레일링 콤마",
    "imports": "임포트",
    "file_structure": "파일 구조",
    "single_line": "한줄 주석",
    "multi_line": "여러줄 주석",
    "docstring": "독스트링",
    "file_header": "파일 헤더",
    "error_handling": "에러 처리",
    "logging": "로깅",
    "type_annotations": "타입 힌트",
    "other": "기타",
}


# ============================================================
# v1.7: 기존 컨벤션 MD 파일 파싱 및 병합 로직
# ============================================================

def parse_existing_convention_md(md_path: str) -> Dict[str, Dict[str, str]]:
    """기존 컨벤션 MD 파일을 파싱하여 카테고리/키/값 구조로 추출.

    Args:
        md_path: 기존 컨벤션 MD 파일 경로

    Returns:
        {category: {key: description}} 형태의 딕셔너리
        예: {"Naming": {"Variables": "snake_case"}, "Formatting": {"Indentation": "4 spaces"}}
    """
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        log(f"  ⚠️ 기존 컨벤션 파일 읽기 실패: {e}")
        return {}

    conventions = {}
    current_category = None

    for line in content.split("\n"):
        line = line.rstrip()

        # ### 카테고리 헤더 (예: ### Naming, ### Formatting)
        cat_match = re.match(r'^###\s+(.+)$', line)
        if cat_match:
            current_category = cat_match.group(1).strip()
            if current_category not in conventions:
                conventions[current_category] = {}
            continue

        # - **키**: 값 (예: - **Variables**: snake_case for all variables)
        item_match = re.match(r'^-\s+\*\*(.+?)\*\*:\s*(.+)$', line)
        if item_match and current_category:
            key = item_match.group(1).strip()
            value = item_match.group(2).strip()
            conventions[current_category][key] = value
            continue

        # 서브 리스트 항목 (  - 값)
        sub_match = re.match(r'^\s+-\s+(.+)$', line)
        if sub_match and current_category:
            # 이전 키의 서브 항목으로 추가 (기타 패턴 등)
            value = sub_match.group(1).strip()
            # "Other" 등 리스트 항목은 _list_N 키로 저장
            list_idx = sum(1 for k in conventions.get(current_category, {}) if k.startswith("_list_"))
            conventions[current_category][f"_list_{list_idx}"] = value

    return conventions


def _normalize_key(key: str) -> str:
    """비교용 키 정규화 (대소문자 무시, 공백/_/- 통일)"""
    return re.sub(r'[\s_\-]+', '_', key.strip().lower())


def merge_conventions(
    existing: Dict[str, Dict[str, str]],
    new_accepted: Dict[str, Dict],
    threshold: int,
) -> Tuple[Dict[str, list], Dict]:
    """기존 컨벤션과 새 분석 결과를 병합.

    Args:
        existing: parse_existing_convention_md()의 반환값
        new_accepted: classify_conventions()에서 accepted로 분류된 컨벤션
        threshold: 채택률 임계값

    Returns:
        (merged_items, merge_report)
        merged_items: {category: [(key, description, status, detail)]} 
            status: "유지" | "신규" | "변경"
            detail: 변경 시 diff 정보
        merge_report: {"kept": int, "added": int, "changed": int, "details": [...]}
    """
    merged = {}
    report = {"kept": 0, "added": 0, "changed": 0, "details": []}

    # 기존 컨벤션의 정규화된 키 → (원본카테고리, 원본키, 원본값) 매핑
    existing_normalized = {}
    for cat, items in existing.items():
        norm_cat = _normalize_key(cat)
        for key, val in items.items():
            if key.startswith("_list_"):
                continue
            norm_key = _normalize_key(key)
            existing_normalized[(norm_cat, norm_key)] = (cat, key, val)

    # 새 분석 결과 순회
    processed_existing_keys = set()
    
    for category, items in new_accepted.items():
        cat_label = category.replace("_", " ").title()
        norm_cat = _normalize_key(cat_label)
        
        if cat_label not in merged:
            merged[cat_label] = []
        
        if isinstance(items, dict):
            for key, val in items.items():
                if isinstance(val, list):
                    # 리스트 항목 (patterns.other 등)
                    label = key.replace("_", " ").title()
                    for item in val:
                        desc = _get_convention_description(item)
                        merged[cat_label].append((label, desc, "신규", ""))
                        report["added"] += 1
                        report["details"].append(f"[신규] {cat_label} > {label}: {desc}")
                    continue

                desc = _get_convention_description(val)
                label = key.replace("_", " ").title()
                norm_key = _normalize_key(label)
                
                lookup_key = (norm_cat, norm_key)
                
                if lookup_key in existing_normalized:
                    # 기존에도 있는 항목 → 비교
                    orig_cat, orig_key, orig_val = existing_normalized[lookup_key]
                    processed_existing_keys.add(lookup_key)
                    
                    # 값 비교 (정규화 후)
                    norm_orig = _normalize_key(orig_val)
                    norm_new = _normalize_key(desc)
                    
                    if norm_orig == norm_new:
                        # 동일 → 유지
                        merged[cat_label].append((label, orig_val, "유지", ""))
                        report["kept"] += 1
                    else:
                        # 다름 → 변경 표시
                        diff_detail = f"기존: {orig_val} → 신규: {desc}"
                        merged[cat_label].append((label, orig_val, "변경", diff_detail))
                        report["changed"] += 1
                        report["details"].append(f"[변경] {cat_label} > {label}: {diff_detail}")
                else:
                    # 신규 항목
                    merged[cat_label].append((label, desc, "신규", ""))
                    report["added"] += 1
                    report["details"].append(f"[신규] {cat_label} > {label}: {desc}")

    # 기존에만 있고 새 분석에 없는 항목 → 유지
    for (norm_cat, norm_key), (orig_cat, orig_key, orig_val) in existing_normalized.items():
        if (norm_cat, norm_key) not in processed_existing_keys:
            if orig_key.startswith("_list_"):
                continue
            cat_label = orig_cat
            if cat_label not in merged:
                merged[cat_label] = []
            merged[cat_label].append((orig_key, orig_val, "유지", ""))
            report["kept"] += 1

    return merged, report


def generate_merged_convention_md(
    merged_items: Dict[str, list],
    merge_report: Dict,
    lang: str,
    project_path: str,
    analyzed_files: int,
    threshold: int,
    merge_source: str,
    analysis: Optional[Dict] = None,
) -> str:
    """병합된 컨벤션 마크다운 생성.

    Args:
        merged_items: merge_conventions()의 merged_items
        merge_report: merge_conventions()의 merge_report
        lang: 언어
        project_path: 프로젝트 경로
        analyzed_files: 분석 파일 수
        threshold: 채택률 기준
        merge_source: 병합 원본 파일 경로
        analysis: 원본 분석 결과 (examples 등 포함)

    Returns:
        마크다운 문자열
    """
    lines = []
    
    # 헤더
    lines.append(f"# {lang.title()} Coding Conventions (Merged)")
    lines.append("")
    lines.append(f"> Auto-generated by {TOOL_NAME}")
    lines.append(f"> Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> Project: {project_path}")
    lines.append(f"> Language: {lang}")
    lines.append(f"> Files analyzed: {analyzed_files}")
    lines.append(f"> Merge source: {merge_source}")
    lines.append(f"> Adoption threshold: {threshold}%")
    if analysis:
        confidence = analysis.get("confidence", "medium")
        lines.append(f"> Confidence: {confidence}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 병합 요약
    lines.append("## 병합 요약")
    lines.append("")
    lines.append(f"| 구분 | 건수 |")
    lines.append(f"|------|------|")
    lines.append(f"| ✅ 유지 | {merge_report['kept']}건 |")
    lines.append(f"| 🆕 신규 | {merge_report['added']}건 |")
    lines.append(f"| 🔄 변경 | {merge_report['changed']}건 |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 프로젝트 컨벤션 (병합 결과)
    lines.append("## 프로젝트 컨벤션")
    lines.append("")

    if not merged_items:
        lines.append("_확정된 컨벤션이 없습니다._")
        lines.append("")
    else:
        for category, items in merged_items.items():
            lines.append(f"### {category}")
            lines.append("")
            for key, desc, status, detail in items:
                if status == "유지":
                    lines.append(f"- **{key}**: {desc}")
                elif status == "신규":
                    lines.append(f"- **{key}**: {desc} `[신규]`")
                elif status == "변경":
                    lines.append(f"- **{key}**: {desc} `[변경]` {detail}")
            lines.append("")

    # 변경 상세 (diff 섹션)
    if merge_report["changed"] > 0 or merge_report["added"] > 0:
        lines.append("## 변경 상세")
        lines.append("")
        for detail in merge_report["details"]:
            lines.append(f"- {detail}")
        lines.append("")

    # Examples (원본 분석에서 가져오기)
    if analysis:
        examples = analysis.get("examples", {})
        good = examples.get("good", [])
        avoid = examples.get("avoid", [])
        if good or avoid:
            lines.append("## Examples")
            lines.append("")
            if good:
                lines.append("### Good")
                for ex in good:
                    lines.append(f"- `{ex}`")
                lines.append("")
            if avoid:
                lines.append("### Avoid")
                for ex in avoid:
                    lines.append(f"- `{ex}`")
                lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*This file was auto-generated by convention-extractor {VERSION} (merge mode).*")
    lines.append("*Review and adjust as needed before committing.*")
    lines.append("")

    return "\n".join(lines)


# ============================================================
# 언어별 컨벤션 파일 생성 (v0.4: Statistics 섹션 제거)
# ============================================================

def generate_per_language_md(
    analysis: Dict,
    lang: str,
    project_path: str,
    analyzed_files: int,
    threshold: int,
) -> str:
    """특정 언어에 대한 컨벤션 마크다운 생성 (v0.4: Statistics 제거)"""
    lines = []
    accepted, rejected = classify_conventions(analysis, threshold)

    # 헤더
    lines.append(f"# {lang.title()} Coding Conventions")
    lines.append("")
    lines.append(f"> Auto-generated by {TOOL_NAME}")
    lines.append(f"> Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> Project: {project_path}")
    lines.append(f"> Language: {lang}")
    lines.append(f"> Files analyzed: {analyzed_files}")
    confidence = analysis.get("confidence", "medium")
    lines.append(f"> Confidence: {confidence}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 확정된 컨벤션
    lines.append("## 프로젝트 컨벤션")
    lines.append("")

    if not accepted:
        lines.append("_확정된 컨벤션이 없습니다._")
        lines.append("")
    else:
        for category, items in accepted.items():
            cat_label = category.replace("_", " ").title()
            lines.append(f"### {cat_label}")
            lines.append("")
            if isinstance(items, dict):
                for key, val in items.items():
                    if isinstance(val, list):
                        label = key.replace("_", " ").title()
                        lines.append(f"- **{label}**:")
                        for item in val:
                            desc = _get_convention_description(item)
                            lines.append(f"  - {desc}")
                    else:
                        desc = _get_convention_description(val)
                        label = key.replace("_", " ").title()
                        lines.append(f"- **{label}**: {desc}")
            lines.append("")

    # Examples
    examples = analysis.get("examples", {})
    good = examples.get("good", [])
    avoid = examples.get("avoid", [])
    if good or avoid:
        lines.append("## Examples")
        lines.append("")
        if good:
            lines.append("### Good")
            for ex in good:
                lines.append(f"- `{ex}`")
            lines.append("")
        if avoid:
            lines.append("### Avoid")
            for ex in avoid:
                lines.append(f"- `{ex}`")
            lines.append("")

    # v0.4: Statistics 섹션 제거 — 통계는 결과 로그에만 포함

    lines.append("---")
    lines.append("")
    lines.append(f"*This file was auto-generated by convention-extractor {VERSION}.*")
    lines.append("*Review and adjust as needed before committing.*")
    lines.append("")

    return "\n".join(lines)


# ============================================================
# 컴플라이언스 체크 (컨벤션 위반 파일 탐지) — v0.4: 한국어 위반 설명
# ============================================================

def run_compliance_check(
    project_path: str,
    analysis_by_lang: Dict[str, Dict],
    api_base: str,
    api_key: str,
    model: str,
    lang_filter: Optional[str] = None,
    verbose: bool = False,
    threshold: int = DEFAULT_ADOPTION_THRESHOLD,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    compliance_batch_size: int = DEFAULT_COMPLIANCE_BATCH_SIZE,
    compliance_batch_timings: Optional[List] = None,
) -> List[Dict]:
    """전체 프로젝트 파일을 컨벤션 대비 검사, 위반 파일 리스트 반환"""
    project = Path(project_path).resolve()
    all_files = find_all_source_files(str(project), lang_filter)
    # v1.8: CLI --lang이 없으면 config.yaml languages 필터 적용
    if not lang_filter and CONFIG_LANGUAGES:
        all_files = filter_files_by_config_languages(all_files, CONFIG_LANGUAGES)

    if not all_files:
        log("  ⚠️ 검사할 파일이 없습니다.")
        return []

    log(f"\n🔎 컴플라이언스 체크: {len(all_files)}개 파일")

    violations_all = []

    # 언어별로 그룹핑
    files_by_lang = defaultdict(list)
    for fp, lang in all_files:
        files_by_lang[lang].append(fp)

    for lang, file_paths in files_by_lang.items():
        analysis = analysis_by_lang.get(lang)
        if not analysis:
            continue

        # 확정 컨벤션만 추출
        accepted, _ = classify_conventions(analysis, threshold)
        if not accepted:
            continue


        # 컨벤션 JSON을 간결하게
        conv_json = json.dumps(accepted, ensure_ascii=False, indent=2)

        # 배치로 파일 체크 (LLM 호출 최소화)
        batch_size = compliance_batch_size
        total_comp_batches = (len(file_paths) + batch_size - 1) // batch_size
        comp_batch_idx = 0
        for i in range(0, len(file_paths), batch_size):
            comp_batch_idx += 1
            batch = file_paths[i:i + batch_size]
            file_blocks = []
            batch_info = []
            for fp in batch:
                content = read_file_safe(fp)
                if not content or len(content.strip()) < 30:
                    continue
                rel = fp.relative_to(project)
                ext = fp.suffix.lstrip(".")
                truncated = content[:8000]
                if len(content) > 8000:
                    truncated += f"\n... (truncated)"
                file_blocks.append(f"### File: {rel}\n```{ext}\n{truncated}\n```")
                batch_info.append((rel, fp))

            if not file_blocks:
                continue

            prompt = f"""You are checking if source files follow the project's coding conventions.

## CRITICAL INSTRUCTION
The conventions below were extracted from THIS project's actual source code. They represent what the majority of files in this project actually do — NOT general industry standards.
Compare each file ONLY against these project-specific conventions. Do NOT flag violations based on PEP8, ESLint defaults, or any external standard that is not listed in the conventions below.

## Project Conventions (only conventions with adoption_rate >= {threshold}%)
```json
{conv_json}
```

## Files to Check
{chr(10).join(file_blocks)}

## Task
For each file, check against the conventions above. List ONLY actual violations against the project conventions listed above.
**Only report violations. Do NOT report items that are already compliant.**

**IMPORTANT**: Each violation must include:
- "category": 컨벤션 카테고리 (한국어). 예: "네이밍", "들여쓰기", "포매팅", "타입힌트", "주석", "임포트", "에러처리" 등
- "detail_ko": 위반 내용을 한국어로 설명. 형식: "위반 사실(구체적 예시) — 프로젝트 표준은 ..."
  예: "함수명이 camelCase(getUserData) — 프로젝트 표준은 snake_case"
  예: "2칸 스페이스 사용 — 프로젝트 표준은 4칸 스페이스"
  예: "타입 힌트 미사용 — 프로젝트 표준은 타입 힌트 사용"

Output ONLY a valid JSON object (no markdown fences):
{{
  "results": [
    {{
      "file": "relative/path",
      "violations": [
        {{"category": "네이밍", "detail_ko": "함수명이 camelCase(getUserData) — 프로젝트 표준은 snake_case"}}
      ]
    }}
  ]
}}

If a file has no violations, still include it with an empty violations array.
"""
            if verbose:
                log(f"    체크 중: {', '.join(str(r) for r, _ in batch_info)}")

            comp_b_start_clock = datetime.now().strftime('%H:%M:%S')
            log(f"    ⏱️ 배치 [{comp_batch_idx}/{total_comp_batches}] 시작 ({comp_b_start_clock})")
            t_comp_batch = time.time()
            result = call_llm(prompt, api_base, api_key, model, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
            comp_b_elapsed = time.time() - t_comp_batch
            comp_b_end_clock = datetime.now().strftime('%H:%M:%S')
            log(f"    ⏱️ 배치 [{comp_batch_idx}/{total_comp_batches}] 완료 ({comp_b_end_clock}) — {comp_b_elapsed:.1f}s")
            if compliance_batch_timings is not None:
                compliance_batch_timings.append((comp_batch_idx, total_comp_batches, comp_b_start_clock, comp_b_end_clock, comp_b_elapsed))
            if result:
                compliance_batch_desc = f"compliance check: {', '.join(str(r) for r, _ in batch_info)}"
                parsed = parse_json_response(result, batch_info=compliance_batch_desc, model=model, prompt_length=len(prompt))
                if parsed and "results" in parsed:
                    for file_result in parsed["results"]:
                        viols = file_result.get("violations", [])
                        if viols:
                            violations_all.append({
                                "file": file_result.get("file", "unknown"),
                                "language": lang,
                                "violations": viols,
                            })
                elif parsed and "violations" in parsed:
                    # 단일 파일 응답 폴백
                    viols = parsed.get("violations", [])
                    if viols and batch_info:
                        violations_all.append({
                            "file": str(batch_info[0][0]),
                            "language": lang,
                            "violations": viols,
                        })

    return violations_all


def write_refactoring_needed(
    violations: List[Dict],
    output_path: str,
    threshold: int,
    total_files: int,
    outlier_paths: Optional[set] = None,
) -> Tuple[int, str]:
    """refactoring_needed_YYYYMMDD_hhmmss.txt 생성 (v0.4: 파일별 정리, 한국어)
    
    Returns:
        (violation_count, full_content) — 위반 파일 수와 전체 내용 문자열
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _outlier_paths = outlier_paths or set()
    lines = []
    lines.append("=" * 58)
    lines.append("리팩토링 필요 파일 목록")
    lines.append(f"분석일시: {now_str}")
    lines.append(f"채택 기준: {threshold}%")
    lines.append("=" * 58)
    lines.append("")

    if not violations:
        lines.append("모든 파일이 컨벤션을 준수합니다 ✅")
        lines.append("")
        content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return 0, content

    # 파일별로 그룹핑
    total_violation_count = 0
    for item in sorted(violations, key=lambda x: x["file"]):
        filepath = item["file"]
        viols = item.get("violations", [])
        # v1.6: 이상치 파일에 태그 붙이기
        is_outlier = any(filepath in op or Path(op).name == filepath for op in _outlier_paths)
        tag = " [이상치]" if is_outlier else ""
        lines.append(f"=== {filepath}{tag} ===")
        for v in viols:
            # v0.4: 한국어 카테고리 + 상세 설명
            category = v.get("category", "")
            detail_ko = v.get("detail_ko", "")
            # 폴백: 이전 형식 호환
            if not category:
                category = v.get("convention", "기타")
            if not detail_ko:
                detail = v.get("detail", "")
                expected = v.get("expected", "")
                if expected:
                    detail_ko = f"{detail} — 프로젝트 표준은 {expected}"
                else:
                    detail_ko = detail
            lines.append(f"[{category}] {detail_ko}")
            total_violation_count += 1
        lines.append("")

    # 요약
    lines.append("=" * 58)
    lines.append("요약")
    lines.append("=" * 58)
    lines.append(f"위반 파일 수: {len(violations)}개 / 전체 {total_files}개")
    lines.append(f"총 위반 건수: {total_violation_count}건")
    lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return len(violations), content


# ============================================================
# 결과 로그 생성 (v0.4: 통합 결과 로그)
# ============================================================

def build_result_log(
    console_lines: List[str],
    analysis_by_lang: Dict[str, Dict],
    static_summary: Dict,
    threshold: int,
    violations: Optional[List[Dict]],
    refactoring_content: str,
    total_project_files: int,
    outlier_info: Optional[List[Tuple[str, int, int]]] = None,
    merge_report: Optional[Dict] = None,
) -> str:
    """통합 결과 로그 문자열 생성"""
    parts = []

    parts.append("=" * 40)
    parts.append("분석 결과 요약")
    parts.append("=" * 40)
    parts.append("")

    # === 콘솔 로그 ===
    parts.append("=== 콘솔 로그 ===")
    for line in console_lines:
        parts.append(line)
    parts.append("")

    # === 추출된 컨벤션 ===
    parts.append("=== 추출된 컨벤션 ===")
    for lang, analysis in sorted(analysis_by_lang.items()):
        accepted, _ = classify_conventions(analysis, threshold)
        if not accepted:
            parts.append(f"{lang.title()}: (확정 컨벤션 없음)")
            continue
        parts.append(f"{lang.title()}:")
        for category, items in accepted.items():
            cat_ko = CATEGORY_KO.get(category, category.replace("_", " ").title())
            if isinstance(items, dict):
                for key, val in items.items():
                    key_ko = CONVENTION_KEY_KO.get(key, key.replace("_", " "))
                    if isinstance(val, list):
                        for item in val:
                            desc = _get_convention_description(item)
                            rate = _get_adoption_rate(item)
                            rate_str = f" (채택률 {rate}%)" if rate is not None else ""
                            parts.append(f"  - {cat_ko} ({key_ko}): {desc}{rate_str}")
                    else:
                        desc = _get_convention_description(val)
                        rate = _get_adoption_rate(val)
                        rate_str = f" (채택률 {rate}%)" if rate is not None else ""
                        parts.append(f"  - {cat_ko} ({key_ko}): {desc}{rate_str}")
        parts.append("")

    # === 정적 분석 통계 ===
    parts.append("=== 정적 분석 통계 ===")
    parts.append(f"전체 파일: {static_summary.get('total_files', 'N/A')}개")
    parts.append(f"전체 라인: {static_summary.get('total_lines', 'N/A')}줄")

    lang_dist = static_summary.get("files_by_lang", {})
    if lang_dist:
        langs = ", ".join(f"{k}: {v}개" for k, v in sorted(lang_dist.items(), key=lambda x: -x[1]))
        parts.append(f"언어 분포: {langs}")

    naming_stats = static_summary.get("naming_stats", {})
    if naming_stats:
        ns = ", ".join(f"{k}: {v}" for k, v in sorted(naming_stats.items(), key=lambda x: -int(str(x[1]).split('(')[0].strip()) if str(x[1]).split('(')[0].strip().isdigit() else -x[1] if isinstance(x[1], (int, float)) else 0))
        parts.append(f"네이밍 패턴 분포: {ns}")

    indent = static_summary.get("indentation")
    if indent:
        parts.append(f"들여쓰기: {indent}")

    p90 = static_summary.get("line_length_p90")
    if p90:
        parts.append(f"라인 길이 P90: {p90}자")

    parts.append("")

    # === v1.7: 병합 정보 ===
    if merge_report:
        parts.append("=== 컨벤션 병합 결과 ===")
        parts.append(f"유지: {merge_report['kept']}건")
        parts.append(f"신규: {merge_report['added']}건")
        parts.append(f"변경: {merge_report['changed']}건")
        if merge_report.get("details"):
            parts.append("상세:")
            for detail in merge_report["details"]:
                parts.append(f"  {detail}")
        parts.append("")

    # === 리팩토링 필요 파일 요약 ===
    parts.append("=== 리팩토링 필요 파일 요약 ===")
    if violations is not None:
        violation_files = len(violations)
        # 카테고리별 위반 수 집계
        category_counts = Counter()
        for item in violations:
            for v in item.get("violations", []):
                cat = v.get("category", v.get("convention", "기타"))
                category_counts[cat] += 1
        parts.append(f"위반 파일: {violation_files}개 / 전체 {total_project_files}개")
        if category_counts:
            parts.append("카테고리별 위반 수:")
            for cat, cnt in category_counts.most_common():
                parts.append(f"  - {cat}: {cnt}건")
    else:
        parts.append("컴플라이언스 체크 스킵됨")
    parts.append("")

    # === v1.6: 이상치 제외 파일 ===
    parts.append("=== 이상치 제외 파일 ===")
    if outlier_info:
        for opath, mismatches, total in outlier_info:
            fname = Path(opath).name
            parts.append(f"  ⚠️ {fname} ({mismatches}/{total} 항목 불일치)")
    else:
        parts.append("  (이상치 없음)")
    parts.append("")

    # === JSON 분석 데이터 ===
    parts.append("=== JSON 분석 데이터 ===")
    for lang, analysis in sorted(analysis_by_lang.items()):
        parts.append(f"--- {lang.title()} ---")
        parts.append(json.dumps(analysis, ensure_ascii=False, indent=2))
        parts.append("")

    # === 리팩토링 상세 ===
    parts.append("=== 리팩토링 상세 ===")
    if refactoring_content:
        parts.append(refactoring_content)
    else:
        parts.append("(해당 없음)")
    parts.append("")

    return "\n".join(parts)


# ============================================================
# 메인 분석 로직
# ============================================================

def run_analysis(
    project_path: str,
    output_path: Optional[str] = None,
    lang_filter: Optional[str] = None,
    api_base: str = DEFAULT_API_BASE,
    api_key: str = DEFAULT_API_KEY,
    model: str = DEFAULT_MODEL,
    max_files: int = DEFAULT_MAX_FILES,
    verbose: bool = False,
    skip_compliance: bool = False,
    threshold: int = DEFAULT_ADOPTION_THRESHOLD,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    merge_file: Optional[str] = None,
):
    """분석 실행"""
    global _logger
    
    project = Path(project_path).resolve()
    
    # 출력 디렉토리 결정
    if output_path:
        output_dir = Path(output_path)
    else:
        output_dir = project
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 타임스탬프 생성
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 로거 초기화 (콘솔 + 파일)
    log_filename = f"extract_convention_result_{timestamp}.log"
    log_path = output_dir / log_filename
    _logger = TeeLogger(str(log_path))
    
    # v1.3: 디버그 로거 초기화 (문제 발생 시에만 파일 생성)
    global _debug_logger
    _debug_logger = DebugLogger(str(output_dir), timestamp)
    
    # v1.8: thinking 모델 자동 보정 제거 — 사용자 설정값 존중
    # config.yaml에서 max_tokens/timeout을 모델에 맞게 직접 설정할 것

    log(f"\n🔍 {TOOL_NAME}")
    log(f"📂 프로젝트: {project}")
    log(f"📡 API: {api_base}")
    log(f"🤖 모델: {model}")
    log(f"📏 채택률 기준: {threshold}%")
    log(f"🌡️ Temperature: {temperature}")
    log(f"📦 Max Tokens: {max_tokens}")
    log(f"⏱️ Timeout: {timeout}s")
    log(f"📝 로그 파일: {log_path}")
    if CONFIG_LANGUAGES:
        log(f"🔤 언어 필터 (config): {', '.join(sorted(CONFIG_LANGUAGES))}")
    if CONFIG_CONVENTIONS:
        log(f"📋 카테고리 필터 (config): {', '.join(sorted(CONFIG_CONVENTIONS))}")

    # v1.7: 병합 모드 안내
    existing_conventions = None
    merge_report_final = None
    if merge_file:
        merge_path = Path(merge_file)
        if not merge_path.is_file():
            log(f"❌ 병합할 컨벤션 파일이 존재하지 않습니다: {merge_file}")
            _logger.close()
            sys.exit(1)
        log(f"🔀 병합 모드: {merge_file}")
        existing_conventions = parse_existing_convention_md(str(merge_path))
        if existing_conventions:
            total_existing = sum(len(items) for items in existing_conventions.values())
            log(f"   기존 컨벤션: {len(existing_conventions)}개 카테고리, {total_existing}개 항목")
        else:
            log(f"   ⚠️ 기존 컨벤션 파일에서 항목을 찾지 못했습니다. 일반 모드로 진행합니다.")
            existing_conventions = None

    # 시간 측정용 변수: {step_name: (start_clock, end_clock, elapsed_seconds)}
    timing = {}
    batch_timings = []  # [(batch_idx, total_batches, start_clock, end_clock, elapsed)]
    compliance_batch_timings = []  # [(batch_idx, total_batches, start_clock, end_clock, elapsed)]
    t_total_start = time.time()
    total_start_clock = datetime.now().strftime('%H:%M:%S')

    # 1. 파일 수집
    step_start_clock = datetime.now().strftime('%H:%M:%S')
    t_step = time.time()
    log(f"⏱️ [파일 수집] 시작 ({step_start_clock})")
    files = find_source_files(str(project), lang_filter)
    
    # v1.8: CLI --lang이 없으면 config.yaml의 languages 섹션으로 필터링
    if not lang_filter and CONFIG_LANGUAGES:
        before_count = len(files)
        files = filter_files_by_config_languages(files, CONFIG_LANGUAGES)
        if before_count != len(files):
            log(f"🔤 config.yaml languages 필터 적용: {before_count} → {len(files)}개")
            log(f"   활성 확장자: {', '.join(sorted(CONFIG_LANGUAGES))}")

    if not files:
        log("❌ 분석할 소스 파일이 없습니다.")
        _logger.close()
        sys.exit(1)

    log(f"📁 발견된 파일: {len(files)}개")

    # 언어별 통계
    lang_counts = Counter(lang for _, lang in files)
    for lang, count in lang_counts.most_common():
        log(f"   {lang}: {count}개")

    # 파일 수 제한
    if len(files) > max_files:
        # v1.8: 언어별 균등 샘플링 + 잔여 할당량 재분배
        lang_file_map = {}
        for lang in lang_counts:
            lang_file_map[lang] = [(f, l) for f, l in files if l == lang]

        per_lang = max(1, max_files // len(lang_counts))
        allocated = {}
        remaining = max_files

        # 1차: 파일 수가 할당량보다 적은 언어는 전부 포함
        for lang in lang_counts:
            allocated[lang] = min(per_lang, len(lang_file_map[lang]))
            remaining -= allocated[lang]

        # 2차: 남은 할당량을 부족하지 않은 언어에 비율 분배
        overflow_langs = {lang: len(lang_file_map[lang]) - allocated[lang]
                          for lang in lang_counts if len(lang_file_map[lang]) > allocated[lang]}
        if overflow_langs and remaining > 0:
            total_overflow = sum(overflow_langs.values())
            for lang in overflow_langs:
                extra = int(remaining * overflow_langs[lang] / total_overflow)
                extra = min(extra, len(lang_file_map[lang]) - allocated[lang])
                allocated[lang] += extra

        # 샘플링
        sampled = []
        for lang in lang_counts:
            lf = lang_file_map[lang]
            n = allocated[lang]
            step = max(1, len(lf) // n) if n > 0 else 1
            sampled.extend(lf[::step][:n])
        files = sampled[:max_files]

    log(f"📊 분석 대상: {len(files)}개")
    step_elapsed = time.time() - t_step
    step_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['파일 수집'] = (step_start_clock, step_end_clock, step_elapsed)
    log(f"⏱️ [파일 수집] 완료 ({step_end_clock}) — {step_elapsed:.1f}s")

    # 2. 정적 분석
    step_start_clock = datetime.now().strftime('%H:%M:%S')
    t_step = time.time()
    log(f"\n⏱️ [정적 분석] 시작 ({step_start_clock})")
    log("⚙️ 정적 분석 중...")
    analyzer = StaticAnalyzer()
    file_contents = []
    file_contents_by_lang = defaultdict(list)

    for file_path, lang in files:
        content = read_file_safe(file_path)
        if not content or len(content.strip()) < 30:
            continue
        analyzer.analyze_file(file_path, content, lang)
        rel = file_path.relative_to(project)
        file_contents.append((rel, lang, content))
        file_contents_by_lang[lang].append((rel, lang, content))

    static_summary = analyzer.get_summary()
    if verbose:
        log(f"   정적 분석 결과: {json.dumps(static_summary, ensure_ascii=False, indent=2)}")

    log(f"   ✅ {len(file_contents)}개 파일 분석 완료")
    step_elapsed = time.time() - t_step
    step_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['정적 분석'] = (step_start_clock, step_end_clock, step_elapsed)
    log(f"⏱️ [정적 분석] 완료 ({step_end_clock}) — {step_elapsed:.1f}s")

    # 2.5. v1.6: 이상치 파일 탐지
    outlier_paths_set = set()  # 이상치 파일 경로 (문자열)
    outlier_info = []  # (path_str, mismatches, total) for logging
    outlier_results = analyzer.detect_outliers(files)
    if outlier_results:
        log(f"\n🔍 이상치(outlier) 파일 탐지:")
        for opath, mismatches, total in outlier_results:
            fname = Path(opath).name
            log(f"   ⚠️ 이상치 제외: {fname} ({mismatches}/{total} 항목 불일치)")
            outlier_paths_set.add(opath)
            outlier_info.append((opath, mismatches, total))

    # 이상치 파일을 LLM 컨벤션 추출 대상에서 제외 (컴플라이언스 체크에는 포함)
    file_contents_for_llm = [
        (rel, lang, content)
        for rel, lang, content in file_contents
        if str(project / rel) not in outlier_paths_set
    ]
    if outlier_paths_set:
        excluded_count = len(file_contents) - len(file_contents_for_llm)
        log(f"   📊 LLM 분석 대상: {len(file_contents_for_llm)}개 (이상치 {excluded_count}개 제외)")

    # 3. LLM 분석 (배치 처리)
    step_start_clock = datetime.now().strftime('%H:%M:%S')
    t_llm_start = time.time()
    log(f"\n⏱️ [LLM 컨벤션 추출] 시작 ({step_start_clock})")
    log("🤖 LLM 분석 중...")

    # SSL 경고 억제
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    batches = []
    for i in range(0, len(file_contents_for_llm), BATCH_SIZE):
        batches.append(file_contents_for_llm[i : i + BATCH_SIZE])

    current_analysis = None
    success_count = 0

    for batch_idx, batch in enumerate(batches, 1):
        log(f"\n  배치 [{batch_idx}/{len(batches)}]")
        for rel, lang, _ in batch:
            log(f"    📄 {rel}")

        # 파일 블록 생성
        file_blocks = []
        for rel, lang, content in batch:
            ext = Path(str(rel)).suffix.lstrip(".")
            truncated = content[:12000]
            if len(content) > 12000:
                truncated += f"\n... (truncated, {len(content)} chars total)"
            file_blocks.append(f"### File: {rel} ({lang})\n```{ext}\n{truncated}\n```")

        file_blocks_str = "\n\n".join(file_blocks)

        if current_analysis is None:
            prompt = build_analysis_prompt(
                static_summary=json.dumps(static_summary, ensure_ascii=False, indent=2),
                file_blocks=file_blocks_str,
                selected_conventions=CONFIG_CONVENTIONS,
            )
        else:
            prompt = MERGE_PROMPT.format(
                previous=json.dumps(current_analysis, ensure_ascii=False, indent=2),
                new_analysis="(analyze from source files below)",
                file_blocks=file_blocks_str,
            )

        if verbose:
            log(f"    프롬프트: {len(prompt)} chars")

        batch_start_clock = datetime.now().strftime('%H:%M:%S')
        log(f"    ⏱️ 배치 [{batch_idx}/{len(batches)}] 시작 ({batch_start_clock})")
        t_batch = time.time()
        result = call_llm(prompt, api_base, api_key, model, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        batch_elapsed = time.time() - t_batch
        batch_end_clock = datetime.now().strftime('%H:%M:%S')
        batch_timings.append((batch_idx, len(batches), batch_start_clock, batch_end_clock, batch_elapsed))
        log(f"    ⏱️ 배치 [{batch_idx}/{len(batches)}] 완료 ({batch_end_clock}) — {batch_elapsed:.1f}s")

        if result:
            parsed = parse_json_response(
                result,
                batch_info=f"배치 {batch_idx}/{len(batches)}",
                model=model,
                prompt_length=len(prompt),
            )
            if parsed:
                current_analysis = parsed
                success_count += 1
                log(f"    ✅ 분석 완료")
            else:
                log(f"    ⚠️ JSON 파싱 실패 (debug 로그 참조)")
                if verbose:
                    log(f"    응답 시작: {result[:200]}...")
        else:
            log(f"    ❌ API 호출 실패")

    step_elapsed = time.time() - t_llm_start
    step_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['LLM 컨벤션 추출'] = (step_start_clock, step_end_clock, step_elapsed)
    log(f"⏱️ [LLM 컨벤션 추출] 완료 ({step_end_clock}) — {step_elapsed:.1f}s")

    # 4. 결과 생성
    if current_analysis is None:
        log("\n❌ LLM 분석 결과가 없습니다.")
        current_analysis = {
            "language": list(lang_counts.keys())[0] if lang_counts else "unknown",
            "conventions": {
                "naming": {"detected_patterns": {"description": str(static_summary.get("naming_stats", {})), "adoption_rate": 50}},
                "formatting": {
                    "indentation": {"description": static_summary.get("indentation", "unknown"), "adoption_rate": 80},
                },
            },
            "confidence": "low (static analysis only)",
        }

    # 4.5. 컨벤션 분류
    step_start_clock = datetime.now().strftime('%H:%M:%S')
    t_step = time.time()
    log(f"⏱️ [컨벤션 분류] 시작 ({step_start_clock})")
    _pre_accepted, _pre_rejected = classify_conventions(current_analysis, threshold)
    step_elapsed = time.time() - t_step
    step_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['컨벤션 분류'] = (step_start_clock, step_end_clock, step_elapsed)
    log(f"⏱️ [컨벤션 분류] 완료 ({step_end_clock}) — {step_elapsed:.1f}s")

    # 5. 언어별 파일 생성 (v0.4: Statistics 제거)
    step_start_clock = datetime.now().strftime('%H:%M:%S')
    t_step = time.time()
    log(f"\n⏱️ [컨벤션 MD 생성] 시작 ({step_start_clock})")
    log(f"📝 언어별 컨벤션 파일 생성 중...")
    detected_langs = list(lang_counts.keys())
    analysis_by_lang = {}
    generated_files = []

    for lang in detected_langs:
        lang_file_count = sum(1 for _, l, _ in file_contents if l == lang)
        if lang_file_count == 0:
            continue

        # 언어별 분석 결과 (현재는 통합 분석에서 공유)
        analysis_by_lang[lang] = current_analysis

        # v1.7: 병합 모드일 경우 병합된 MD 생성
        if existing_conventions:
            accepted, _ = classify_conventions(current_analysis, threshold)
            merged_items, merge_report_final = merge_conventions(existing_conventions, accepted, threshold)

            merged_filename = f"{lang}_convention_merged.md"
            merged_filepath = output_dir / merged_filename
            merged_md = generate_merged_convention_md(
                merged_items=merged_items,
                merge_report=merge_report_final,
                lang=lang,
                project_path=str(project),
                analyzed_files=lang_file_count,
                threshold=threshold,
                merge_source=merge_file,
                analysis=current_analysis,
            )
            with open(merged_filepath, "w", encoding="utf-8") as f:
                f.write(merged_md)
            generated_files.append(merged_filepath)
            log(f"   ✅ {merged_filename} (병합 결과: 유지 {merge_report_final['kept']}, 신규 {merge_report_final['added']}, 변경 {merge_report_final['changed']})")

        # 기본 컨벤션 MD도 항상 생성
        filename = f"{lang}_convention.md"
        filepath = output_dir / filename
        md_content = generate_per_language_md(
            current_analysis, lang, str(project), lang_file_count, threshold
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        generated_files.append(filepath)
        log(f"   ✅ {filename} ({lang_file_count}개 파일)")

    step_elapsed = time.time() - t_step
    step_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['컨벤션 MD 생성'] = (step_start_clock, step_end_clock, step_elapsed)
    log(f"⏱️ [컨벤션 MD 생성] 완료 ({step_end_clock}) — {step_elapsed:.1f}s")

    # 6. JSON 저장 (디버깅/MCP 연동용)
    json_path = output_dir / "conventions.json"
    accepted, rejected = classify_conventions(current_analysis, threshold)
    # v1.6: 이상치 정보를 JSON에 포함
    outlier_files_json = [
        {"file": Path(op).name, "path": op, "mismatches": m, "total": t}
        for op, m, t in outlier_info
    ]
    json_data = {
        "project": str(project),
        "timestamp": datetime.now().isoformat(),
        "tool_version": VERSION,
        "model": model,
        "adoption_threshold": threshold,
        "files_analyzed": len(file_contents),
        "files_analyzed_for_llm": len(file_contents_for_llm),
        "outlier_files": outlier_files_json,
        "static_summary": static_summary,
        "analysis": current_analysis,
        "accepted_conventions": accepted,
        "rejected_conventions": rejected,
    }
    # v1.7: 병합 정보를 JSON에 포함
    if merge_report_final:
        json_data["merge_info"] = {
            "merge_source": merge_file,
            "kept": merge_report_final["kept"],
            "added": merge_report_final["added"],
            "changed": merge_report_final["changed"],
            "details": merge_report_final.get("details", []),
        }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    log(f"   📋 JSON: {json_path}")

    # 7. 컴플라이언스 체크
    refactoring_filename = f"refactoring_needed_{timestamp}.txt"
    refactoring_path = output_dir / refactoring_filename
    violations = None
    refactoring_content = ""
    _all_project_files = find_all_source_files(str(project), lang_filter)
    if not lang_filter and CONFIG_LANGUAGES:
        _all_project_files = filter_files_by_config_languages(_all_project_files, CONFIG_LANGUAGES)
    total_project_files = len(_all_project_files)
    
    if not skip_compliance:
        step_start_clock = datetime.now().strftime('%H:%M:%S')
        t_step = time.time()
        log(f"\n⏱️ [컴플라이언스 체크] 시작 ({step_start_clock})")
        log("🔎 컴플라이언스 체크 시작...")
        violations = run_compliance_check(
            str(project),
            analysis_by_lang,
            api_base,
            api_key,
            model,
            lang_filter,
            verbose,
            threshold,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
            compliance_batch_timings=compliance_batch_timings,
        )
        step_elapsed_comp = time.time() - t_step
        step_end_clock_comp = datetime.now().strftime('%H:%M:%S')

        refactor_start_clock = datetime.now().strftime('%H:%M:%S')
        t_refactor = time.time()
        log(f"⏱️ [리팩토링 파일 생성] 시작 ({refactor_start_clock})")
        violation_count, refactoring_content = write_refactoring_needed(
            violations, str(refactoring_path), threshold, total_project_files,
            outlier_paths=outlier_paths_set,
        )
        refactor_elapsed = time.time() - t_refactor
        refactor_end_clock = datetime.now().strftime('%H:%M:%S')
        timing['리팩토링 파일 생성'] = (refactor_start_clock, refactor_end_clock, refactor_elapsed)
        log(f"⏱️ [리팩토링 파일 생성] 완료 ({refactor_end_clock}) — {refactor_elapsed:.1f}s")

        # 컴플라이언스 체크 시간에서 리팩토링 파일 생성 시간 제외
        comp_elapsed = step_elapsed_comp
        timing['컴플라이언스 체크'] = (step_start_clock, step_end_clock_comp, comp_elapsed)

        if violation_count > 0:
            log(f"   ⚠️ {violation_count}개 파일에서 컨벤션 위반 발견")
            log(f"   📄 {refactoring_path}")
        else:
            log(f"   ✅ 모든 파일이 컨벤션을 준수합니다")
        log(f"⏱️ [컴플라이언스 체크] 완료 ({step_end_clock_comp}) — {comp_elapsed:.1f}s")
    else:
        log("\n⏭️ 컴플라이언스 체크 스킵 (--skip-compliance)")

    # 전체 소요 시간 계산 및 요약 테이블 출력
    total_elapsed = time.time() - t_total_start
    total_end_clock = datetime.now().strftime('%H:%M:%S')
    timing['총 소요'] = (total_start_clock, total_end_clock, total_elapsed)

    log(f"\n⏱️ 단계별 소요 시간:")
    step_order = ['파일 수집', '정적 분석', 'LLM 컨벤션 추출', '컨벤션 분류', '컨벤션 MD 생성', '컴플라이언스 체크', '리팩토링 파일 생성']
    for step_name in step_order:
        if step_name in timing:
            s_clock, e_clock, elapsed = timing[step_name]
            log(f"  {step_name + ':':20s} {s_clock} ~ {e_clock}  {elapsed:>8.1f}s")
            if step_name == 'LLM 컨벤션 추출' and batch_timings:
                for b_idx, b_total, b_s, b_e, b_elapsed in batch_timings:
                    label = f"배치 {b_idx}/{b_total}:"
                    log(f"    {label:18s} {b_s} ~ {b_e}  {b_elapsed:>8.1f}s")
            if step_name == '컴플라이언스 체크' and compliance_batch_timings:
                for b_idx, b_total, b_s, b_e, b_elapsed in compliance_batch_timings:
                    label = f"배치 {b_idx}/{b_total}:"
                    log(f"    {label:18s} {b_s} ~ {b_e}  {b_elapsed:>8.1f}s")
    log(f"  {'─' * 45}")
    s_clock, e_clock, elapsed = timing['총 소요']
    log(f"  {'총 소요:':20s} {s_clock} ~ {e_clock}  {elapsed:>8.1f}s")

    # 완료 요약
    log(f"\n{'='*50}")
    log(f"✅ {TOOL_NAME} 완료!")
    log(f"📊 분석 파일: {len(file_contents)}개, LLM 호출: {success_count}회")
    log(f"📏 채택률 기준: {threshold}%")
    if merge_file and merge_report_final:
        log(f"🔀 병합 결과: 유지 {merge_report_final['kept']}, 신규 {merge_report_final['added']}, 변경 {merge_report_final['changed']}")
    log(f"📁 출력 디렉토리: {output_dir}")
    log(f"   생성된 파일:")
    for gf in generated_files:
        log(f"   - {gf.name}")
    log(f"   - conventions.json")
    if not skip_compliance:
        log(f"   - {refactoring_filename}")
    log(f"   - {log_filename}")
    log(f"{'='*50}")

    # 8. 통합 결과 로그 재작성 (v0.4: 콘솔 로그 + 컨벤션 + 통계 + 리팩토링)
    full_log = build_result_log(
        console_lines=_logger.console_lines,
        analysis_by_lang=analysis_by_lang,
        static_summary=static_summary,
        threshold=threshold,
        violations=violations,
        refactoring_content=refactoring_content,
        total_project_files=total_project_files,
        outlier_info=outlier_info,
        merge_report=merge_report_final,
    )
    _logger.rewrite_log(full_log)

    # v1.3: 디버그 로그 정보 출력
    if _debug_logger and _debug_logger.has_issues:
        log(f"   ⚠️ 문제 발생 — 디버그 로그: debug_{timestamp}.log")

    # 로거 닫기
    _logger.close()
    if _debug_logger:
        _debug_logger.close()
    
    return str(output_dir)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=TOOL_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            환경변수:
              CONVENTION_API_BASE            API 엔드포인트 (기본: http://localhost:11434/v1)
              CONVENTION_API_KEY             API 키 (기본: no-key)
              CONVENTION_MODEL               모델명 (기본: qwen2.5-coder:32b)
              CONVENTION_ADOPTION_THRESHOLD  채택률 임계값 (기본: 90)

            예시:
              %(prog)s /path/to/project
              %(prog)s /path/to/project -o output_dir/
              %(prog)s /path/to/project --lang python --max-files 30
              %(prog)s /path/to/project --threshold 80
              %(prog)s /path/to/project --api-base https://api.openai.com/v1 --api-key sk-xxx --model gpt-4o
              %(prog)s /path/to/project --skip-compliance
              %(prog)s /path/to/project --merge existing_convention.md
        """),
    )
    parser.add_argument(
        "project",
        help="분석할 프로젝트 디렉토리 경로",
    )
    parser.add_argument(
        "-o", "--output",
        help="출력 디렉토리 경로 (기본: <project>/)",
    )
    parser.add_argument(
        "--lang", "-l",
        choices=list(LANG_EXTENSIONS.keys()),
        help="특정 언어만 분석 (기본: 전체)",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"API 엔드포인트 (기본: {DEFAULT_API_BASE})",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help="API 키",
    )
    parser.add_argument(
        "--model", "-m",
        default=DEFAULT_MODEL,
        help=f"모델명 (기본: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--max-files", "-n",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="분석할 최대 파일 수 (기본: %d)" % DEFAULT_MAX_FILES,
    )
    parser.add_argument(
        "--threshold", "-t",
        type=int,
        default=DEFAULT_ADOPTION_THRESHOLD,
        help=f"채택률 임계값 %% (기본: {DEFAULT_ADOPTION_THRESHOLD})",
    )
    parser.add_argument(
        "--skip-compliance",
        action="store_true",
        help="컴플라이언스 체크 스킵 (컨벤션 추출만 수행)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"LLM 온도 (기본: {DEFAULT_TEMPERATURE})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"API 응답 대기 시간 초 (기본: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"LLM 응답 최대 토큰 수 (기본: {DEFAULT_MAX_TOKENS})",
    )
    parser.add_argument(
        "--merge",
        default=None,
        help="기존 컨벤션 MD 파일과 병합 (예: existing_convention.md)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 출력",
    )

    args = parser.parse_args()

    # v1.4: CLI -v 옵션이 있으면 우선, 없으면 config에서 읽기
    verbose = args.verbose or DEFAULT_VERBOSE

    run_analysis(
        project_path=args.project,
        output_path=args.output,
        lang_filter=args.lang,
        api_base=args.api_base,
        api_key=args.api_key,
        model=args.model,
        max_files=args.max_files,
        verbose=verbose,
        skip_compliance=args.skip_compliance,
        threshold=args.threshold,
        temperature=args.temperature,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        merge_file=args.merge,
    )


if __name__ == "__main__":
    main()