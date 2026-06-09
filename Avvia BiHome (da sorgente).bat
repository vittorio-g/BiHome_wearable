@echo off
REM ============================================================================
REM  BiHome Wearable — launcher da sorgente
REM ============================================================================
REM  Avvia BiHome_wearable.py usando il Python installato sul sistema (di solito
REM  Anaconda). Equivalente funzionale di "BiHome Wearable.exe" ma esegue il
REM  codice sorgente, quindi NON viene bloccato da Smart App Control / SmartScreen
REM  (che bloccano solo eseguibili non firmati).
REM
REM  Come usarlo:
REM    - Fai doppio click su questo file, oppure
REM    - Trascinalo sul Desktop come "Crea collegamento qui" (tasto destro)
REM
REM  Per debug, lancia da terminale: vedrai i messaggi a video.
REM ============================================================================

REM Cambia working directory a quella di questo .bat (gestisce drive lettera).
cd /d "%~dp0"

REM Preferisci pythonw (senza finestra console nera) se disponibile.
where pythonw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Avvio BiHome con pythonw ^(senza console^)...
    start "" pythonw "BiHome_wearable.py"
    exit /b 0
)

REM Fallback: python con console visibile (utile per diagnosi).
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERRORE] Nessun interprete Python trovato nel PATH.
    echo.
    echo Installa Python ^(o Anaconda^) e assicurati che sia nel PATH di Windows,
    echo poi rilancia questo file. In alternativa, usa "BiHome Wearable.exe" dopo
    echo aver disattivato Smart App Control ^(vedi DISTRIBUTION.md^).
    echo.
    pause
    exit /b 1
)

echo Avvio BiHome con python ^(console visibile^)...
python "BiHome_wearable.py"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo BiHome ha terminato con codice errore %ERRORLEVEL%.
    pause
)
