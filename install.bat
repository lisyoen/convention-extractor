@echo off
chcp 949 >nul
echo === convention-extractor v1.7.1 install ===
echo.

REM 1. Python 확인
where python >nul 2>&1
if errorlevel 1 goto nopython

python --version
echo [OK] Python found
echo.

REM 2. 모듈 확인
echo Checking modules...
python -c "import requests; import yaml; print('[OK] modules loaded')"
if errorlevel 1 goto modinstall

:configcheck
REM 3. config.yaml 자동 생성
echo.
if not exist "%~dp0config.yaml" (
    if exist "%~dp0config.example.yaml" (
        copy "%~dp0config.example.yaml" "%~dp0config.yaml" >nul
        echo [OK] config.yaml created
    ) else (
        echo [WARN] config.example.yaml not found
    )
) else (
    echo [OK] config.yaml ready
)

echo.
echo === Install complete ===
echo.
pause
goto end

:modinstall
echo.
echo [INFO] 필수 모듈 오프라인 설치 중...
pip install --no-index --find-links="%~dp0wheels" requests pyyaml 2>nul
if errorlevel 1 goto modfail

echo [OK] 모듈 설치 완료
echo.
REM config.yaml 생성으로 돌아감
goto configcheck

:nopython
echo [ERROR] Python을 찾을 수 없습니다.
echo   관리자에게 문의하세요.
pause
goto end

:modfail
echo.
echo [ERROR] 모듈 설치에 실패했습니다.
echo   관리자에게 문의하세요.
echo.
pause

:end
