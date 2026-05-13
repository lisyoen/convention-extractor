# convention-extractor

[![License: CC0-1.0](https://img.shields.io/badge/License-CC0_1.0-lightgrey.svg)](https://creativecommons.org/publicdomain/zero/1.0/)

Automatically extract coding conventions from a source-code directory using any
OpenAI-compatible LLM API (OpenAI, Ollama, vLLM, LiteLLM, Azure OpenAI, ...).

The tool walks the project tree, samples files per language, asks an LLM to
distill recurring patterns into structured conventions, then runs a fast
**static** compliance checker against the full codebase to flag files that
deviate.

- Korean usage guide: [`USAGE.ko.md`](USAGE.ko.md)
- Korean technical doc: [`TECHNICAL.ko.md`](TECHNICAL.ko.md)

## Features

- **Per-language convention files** — `python_convention.md`, `java_convention.md`, ... pure rules only, ready to drop into a Continue/Cline/Cursor system prompt.
- **Adoption threshold (default 90%)** — only patterns observed in ≥N% of sampled files are promoted to a convention, so the output isn't dominated by one quirky file.
- **Static compliance checker** — regex/AST-based, no LLM cost. Handles 100k–500k file repos in ~1–2 hours instead of days.
- **`.convention-ignore`** — exclude vendored sources (kernels, third-party trees, generated code) with a `.gitignore`-style file.
- **Thinking-model friendly** — auto-strips `<think>...</think>` blocks, raises `max_tokens` / `timeout` for reasoning models.
- **Debug log on failure only** — clean runs leave no log; failures drop a `debug_*.log` with the raw response and traceback.
- **Air-gapped install** — ships with wheels for `requests` / `pyyaml` so it installs on an offline host.

## Supported languages

C / C++, Python, JavaScript / TypeScript / Vue, Java, Kotlin, Go, Rust.

## Requirements

- Python 3.6+
- `requests`, `pyyaml`

## Install

```bash
chmod +x install.sh
./install.sh
```

`install.sh` verifies Python and required modules, then creates `config.yaml`
from `config.example.yaml` if it doesn't exist. It does **not** download
anything — for an air-gapped host, pre-stage wheels:

```bash
# On an internet-connected machine
pip download requests pyyaml -d ./wheels/

# On the air-gapped target
pip install --no-index --find-links=./wheels/ requests pyyaml
```

For Windows, run `install.bat` instead.

## Configure

```bash
cp config.example.yaml config.yaml
# edit api_base, api_key, model
```

Settings precedence: **CLI option > environment variable > `config.yaml` > built-in default**.

| Key | Env var | Default | Notes |
|---|---|---|---|
| `api_base` | `CONVENTION_API_BASE` | `http://localhost:11434/v1` | OpenAI-compatible `/v1/chat/completions` endpoint |
| `api_key` | `CONVENTION_API_KEY` | `no-key` | Ollama accepts any string |
| `model` | `CONVENTION_MODEL` | `qwen2.5-coder:32b` | Any model your endpoint serves |
| `max_files` | — | `1000` | Cap on files sent to the LLM (static check still runs on all) |
| `batch_size` | — | `5` | Files per LLM call. Use 1–2 for small-context models, 8–10 for 128k+ |
| `compliance_batch_size` | — | `3` | Files per compliance LLM call (legacy mode) |
| `adoption_threshold` | `CONVENTION_ADOPTION_THRESHOLD` | `90` | Minimum % of files a pattern must appear in |
| `timeout` | — | `180` | Seconds per LLM request |
| `max_tokens` | — | `4096` | Bump to `16384`+ for thinking models |
| `temperature` | — | `0.2` | Keep low for deterministic analysis |
| `max_file_lines` | — | `400` | Truncate longer files |
| `max_file_size` | — | `50000` | Skip files larger than this (bytes) |
| `exclude_dirs` | — | `[]` | Extra dirs on top of built-in ignores |
| `verbose` | — | `false` | Equivalent to `-v` |

## Usage

```bash
# Basic — full extraction + static compliance check
python3 extract_convention.py /path/to/project

# Separate output directory
python3 extract_convention.py /path/to/project -o output_dir/

# Stricter threshold
python3 extract_convention.py /path/to/project --threshold 95

# Single language
python3 extract_convention.py /path/to/project --lang python

# Skip the compliance phase
python3 extract_convention.py /path/to/project --skip-compliance

# Custom ignore file
python3 extract_convention.py /path/to/project --ignore-file .myignore

# Override LLM settings on the command line
python3 extract_convention.py /path/to/project \
    --api-base https://api.openai.com/v1 \
    --api-key sk-xxx \
    --model gpt-4o

# Merge with an existing convention.md
python3 extract_convention.py /path/to/project --merge existing_convention.md
```

## Excluding directories

Three sources are merged (union):

1. **Built-in** — `node_modules`, `.git`, `__pycache__`, `build`, `dist`, `target`, `bin`, ... always on.
2. **`config.yaml`** — `exclude_dirs:` list.
3. **`.convention-ignore`** at the project root, `.gitignore`-style:

   ```
   # Linux kernel sources bundled in the repo
   kernel/
   linux-kernel/
   drivers/

   # Third-party
   third_party/
   vendor/
   external/
   ```

`--ignore-file path/to/file` overrides the default location.

## Output

| File | Description |
|---|---|
| `{lang}_convention.md` | Per-language rules, ready to feed an LLM coding assistant as a system prompt |
| `conventions.json` | Structured analysis with adoption percentages (for MCP / programmatic use) |
| `refactoring_needed_YYYYMMDD_hhmmss.txt` | Files that violate the extracted conventions, with Korean explanations |
| `extract_convention_result_YYYYMMDD_hhmmss.log` | Combined run log: console + conventions + stats + refactoring summary |
| `debug_YYYYMMDD_hhmmss.log` | Written **only on failure** (JSON parse error, API timeout, file read error) |

## How the static checker works

The LLM-extracted rules are translated into deterministic regex/pattern
matchers covering:

- **Naming**: function / class / variable names against `snake_case`, `camelCase`, `PascalCase`, `UPPER_SNAKE`.
- **Indentation**: tabs vs spaces, width.
- **Brace style**: K&R vs Allman.
- **Semicolons** (JS): used or omitted.
- **Quotes** (JS): single vs double.
- **Type hints** (Python): annotation presence on function signatures.
- **Pointer style** (C): `int*` vs `int *`.
- **Header guards** (C): `#pragma once` vs `#ifndef`.
- **Docstring language** (Python): Korean vs English.

Each rule only fires on languages where it applies. Binary files and files over
100KB are skipped automatically.

## Performance

| Project size | LLM-based check (older) | Static check (this tool) |
|---|---|---|
| 10,000 files | ~3 hours | ~10 minutes |
| 100,000 files | ~30+ hours | ~1 hour |
| 500,000 files | impractical | ~2 hours |

API cost for the static phase: zero.

## Recommended models

Anything OpenAI-compatible works. The tool has been exercised with:

- A fast coding-specialised model (~30B) — recommended for day-to-day runs.
- A mid-sized coding model (~24B) — good balance of cost and detail.
- A large reasoning model (200B+ MoE) — highest quality but very slow; the
  thinking trace can easily push wall-clock past the timeout.

Set the model name in `config.yaml` to whatever your endpoint serves.

## License

This work is dedicated to the public domain under the
[Creative Commons CC0 1.0 Universal Public Domain Dedication](https://creativecommons.org/publicdomain/zero/1.0/).

To the extent possible under law, the author has waived all copyright and
related or neighboring rights to this work. You may copy, modify, distribute,
and use this work, even for commercial purposes, all without asking
permission.

See [`LICENSE`](LICENSE) for the full legal text.

### TL;DR

- **You can**: use, copy, modify, distribute, sublicense, and sell this
  software — for any purpose, commercial or non-commercial — without
  attribution and without asking.
- **No warranty**: the work is provided "as-is", without warranties of any
  kind.
- **No trademark/patent grant**: CC0 does not waive trademark or patent
  rights, and the author makes no patent claims either way.

Attribution is not required, but if you find this useful, a mention is
appreciated.
