# Convention Extractor v1.7 사용 가이드

## 1. 개요

소스코드에서 코딩 컨벤션을 자동 추출하여 AI Code Agent(.rules) 프롬프트로 생성하는 도구입니다.

- **지원 언어**: C, Python, JavaScript, Java, TypeScript, Go, Kotlin (7개)
- **채택률 기준**: 90% 이상 사용 패턴만 프로젝트 컨벤션으로 인정
- **컴플라이언스 체크**: 정적분석 기반 (50만 파일 규모 대응)
- **thinking 모델 자동 지원**: Qwen3, QwQ, DeepSeek-R1 등 자동 감지
- **이상치 자동 탐지**: 프로젝트 스타일과 다른 파일을 자동 감지하여 분석 오염 방지 (v1.6)
- **컨벤션 병합(Merge)**: 기존 확정 컨벤션 파일과 새 분석 결과를 자동 병합, diff 표시 (v1.7)

## 2. 설치

### 2.1 다운로드
```
convention-extractor-v1.7.zip 압축 해제
```

### 2.2 패키지 설치
```bash
# Windows (오프라인)
install.bat

# Linux/Mac
./install.sh
```

### 2.3 config.yaml 설정
```yaml
# API 설정 (필수)
api_base: "http://your-llm-host.example.com:4000/v1"
api_key: "sk-your-api-key-here"
model: "Qwen/your-coding-model-A3B-Instruct"

# 타임아웃 (선택, 기본 180초)
timeout: 300

# 채택률 기준 (선택, 기본 90)
adoption_threshold: 90

# 제외 폴더 (선택)
exclude_dirs:
  - node_modules
  - __pycache__
  - .git
  - build
  - dist
```

※ config.example.yaml에 모델 3종 프리셋이 주석으로 포함되어 있습니다.
  사용할 모델의 주석만 해제하면 바로 사용 가능합니다.

## 3. 실행

### 3.1 기본 실행
```bash
python extract_convention.py <프로젝트경로> -o <출력경로>
```

### 3.2 실행 예시
```bash
# 현재 폴더의 프로젝트 분석, result 폴더에 출력
python extract_convention.py . -o result

# 특정 프로젝트 분석
python extract_convention.py C:\Projects\my-project -o C:\Projects\result

# 컴플라이언스 체크 생략 (분석만)
python extract_convention.py . -o result --skip-compliance

# 타임아웃 변경 (대형 모델 사용 시)
python extract_convention.py . -o result --timeout 500

# 특정 언어만 분석
python extract_convention.py . -o result --lang python

# 모델 직접 지정
python extract_convention.py . -o result -m "your-org/your-coding-model"

# 기존 컨벤션 파일과 병합 (v1.7)
python extract_convention.py . -o result --merge result/python_convention.md
```

### 3.3 주요 옵션
| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-o, --output` | 출력 디렉토리 | 프로젝트 경로 |
| `--timeout` | API 타임아웃 (초) | 180 |
| `--threshold` | 채택률 기준 (%) | 90 |
| `--temperature` | LLM 생성 온도 (0.0~1.0) | 0.2 |
| `--max-tokens` | LLM 최대 응답 토큰 수 | 4096 |
| `--skip-compliance` | 컴플라이언스 체크 생략 | false |
| `--lang` | 특정 언어만 분석 | 전체 |
| `--max-files` | 최대 분석 파일 수 | 1000 |
| `--verbose` | 상세 로그 출력 | false |
| `--outlier-threshold` | 이상치 판정 기준 (불일치 항목 수) | 3 |
| **`--merge`** | **기존 컨벤션 MD 파일과 병합 (v1.7)** | **없음** |

### 3.4 고급 설정 (config.yaml)
| 설정 | 설명 | 기본값 |
|------|------|--------|
| `max_tokens` | LLM 최대 토큰 수 | 4096 |
| `temperature` | 생성 온도 | 0.2 |
| `timeout` | LLM 응답 대기 (초) | 180 |
| `compliance_batch_size` | 컴플라이언스 체크 배치 크기 | 3 |
| `max_file_lines` | 파일당 최대 분석 라인 | 400 |
| `max_file_size` | 파일당 최대 크기 (bytes) | 50000 |
| `verbose` | 상세 출력 | false |
| `outlier_threshold` | 이상치 판정 기준 (3~5) | 3 |

※ thinking 모델(Qwen3 등) 사용 시 max_tokens/timeout이 자동 보정됩니다 (v1.5)

## 4. 출력물

실행 완료 시 출력 디렉토리에 다음 파일이 생성됩니다:

| 파일 | 설명 |
|------|------|
| `python_convention.md` | Python 코딩 컨벤션 (.rules 파일로 사용) |
| `java_convention.md` | Java 코딩 컨벤션 |
| `c_convention.md` | C 코딩 컨벤션 |
| `*_convention_merged.md` | 기존 컨벤션과 병합된 결과 (`--merge` 사용 시에만 생성, v1.7) |
| `conventions.json` | 전체 분석 결과 (JSON, 병합 정보 포함) |
| `refactoring_needed_*.txt` | 컨벤션 불일치 파일 목록 (이상치 파일은 [이상치] 태그 표시) |
| `extract_convention_result_*.log` | 실행 로그 (단계별 소요 시간, 병합 요약 포함) |
| `debug_*.log` | 디버그 로그 (문제 발생 시에만 생성) |

## 5. 컨벤션 병합 (v1.7 신규)

### 5.1 병합이란?
기존에 추출하여 프로젝트에서 공식 채택한 컨벤션 파일(`*_convention.md`)을 입력으로 받아, 새로운 분석 결과와 자동으로 비교·병합하는 기능입니다.

### 5.2 사용법
```bash
# 기본 사용
python extract_convention.py <프로젝트> -o result --merge <기존_컨벤션_파일.md>

# 예시: 이전에 추출한 python_convention.md와 병합
python extract_convention.py . -o result --merge result/python_convention.md

# 다른 모델로 재분석 후 기존 결과와 병합
python extract_convention.py . -o result -m "gpt-4o" --merge result/python_convention.md
```

### 5.3 병합 결과
병합된 파일(`*_convention_merged.md`)에는 다음이 포함됩니다:

| 표시 | 의미 |
|------|------|
| (표시 없음) | 기존 항목이 그대로 유지됨 |
| `[신규]` | 새 분석에서 새로 발견된 항목 |
| `[변경]` | 기존과 다른 값이 감지됨 (diff 표시) |

**병합 요약 테이블** 예시:
```
| 구분 | 건수 |
|------|------|
| ✅ 유지 | 12건 |
| 🆕 신규 | 3건 |
| 🔄 변경 | 1건 |
```

### 5.4 병합 동작 원칙
- **기존 항목 우선 보존**: 팀에서 리뷰하고 확정한 기존 규칙은 자동으로 덮어쓰지 않음
- **신규 항목 자동 추가**: 새 분석에서 발견됐지만 기존에 없던 항목은 `[신규]` 태그로 추가
- **변경 감지**: 기존과 다른 값이면 기존 값을 유지하되 diff 정보 표시
- **삭제하지 않음**: 기존에만 있고 새 분석에 없는 항목도 유지
- **기본 MD도 생성**: `--merge` 사용 시에도 `*_convention.md`(새 분석 결과만)는 항상 생성

### 5.5 사용 시나리오
1. **모델 업그레이드**: 더 좋은 모델로 재분석하면서 기존 컨벤션을 안전하게 확장
2. **코드베이스 성장**: 새로운 코드가 추가된 후 새로운 패턴을 기존 컨벤션에 추가
3. **설정 조정**: `threshold`를 변경하여 재분석 후 기존 결과와 비교
4. **변경 추적**: 컨벤션이 어떻게 변화했는지 diff로 확인

## 6. AI Code Agent 연동

### 6.1 Cline (.clinerules)
생성된 `{언어}_convention.md` 파일을 프로젝트의 `.clinerules/` 폴더에 복사:
```bash
mkdir .clinerules
cp result/python_convention.md .clinerules/
```

### 6.2 Continue (.continuerules)
```bash
mkdir .continuerules
cp result/java_convention.md .continuerules/
```

## 7. 제외 설정 (.convention-ignore)

프로젝트 루트에 `.convention-ignore` 파일 생성:
```
# 테스트 코드 제외
tests/
test_*.py

# 자동 생성 코드 제외
generated/
*_pb2.py

# 벤더 코드 제외
vendor/
third_party/
```

## 8. 권장 모델

| 모델 | 속도 | 한국어 | 품질 | 비고 |
|------|------|--------|------|------|
| your-coding-model | ⚡ 빠름 | ✅ | 핵심 추출 | **권장** |
| your-coding-model-fast | 보통 | ❌ | 균형 | 상세 분석 |
| your-reasoning-model | ⚠️ 매우 느림 | 혼합 | 오탐 있음 | thinking 모델, 대규모 프로젝트 비추천 |

⚠️ your-reasoning-model는 thinking 모델 특성상 소요 시간이 매우 깁니다.
  대규모 프로젝트에서는 your-coding-model 또는 your-coding-model-fast을 권장합니다.

## 9. 문의

관리자 (admin@example.com)

---
*auto-generated by Convention Extractor v1.7*
