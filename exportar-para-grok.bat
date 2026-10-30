@echo off
REM Doble clic en este archivo y hace todo: actualiza el codigo, genera el paquete
REM y abre la carpeta con los archivos listos para subir a Grok.
REM
REM Sin acentos ni simbolos raros a proposito: la consola de Windows usa cp1252 por
REM defecto y una tilde aqui se ve como basura o rompe la linea.

setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   Exportar el contexto de GC Realty
echo ============================================
echo.

echo [1 de 3] Trayendo la ultima version del codigo...
git pull origin claude/chicagoland-landlord-advisor-xeqzho
if errorlevel 1 (
    echo.
    echo    No se pudo actualizar. Puede ser que tengas cambios sin guardar
    echo    o que no haya internet. Sigo con el codigo que ya tienes.
    echo.
)

echo.
echo [2 de 3] Generando el paquete...
echo.

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m markai export
if errorlevel 1 (
    echo.
    echo ============================================
    echo   Algo fallo. Lee el mensaje de arriba.
    echo ============================================
    echo.
    echo   Si dice "Nothing indexed to export", primero corre:
    echo       %PY% -m markai ingest
    echo.
    pause
    exit /b 1
)

echo.
echo [3 de 3] Abriendo la carpeta con los archivos...
start "" "%cd%\data"

echo.
echo ============================================
echo   Listo. Sube los archivos gc-background*.md
echo   a Grok como adjuntos.
echo ============================================
echo.
pause
