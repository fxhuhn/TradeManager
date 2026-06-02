from pathlib import Path
import pytest
import structlog
from app.core.logging_setup import configure_logging


@pytest.mark.asyncio
async def test_configure_logging_creates_file_and_writes(tmp_path: Path) -> None:
    """
    Verifiziert, dass configure_logging() die Logdatei anlegt
    und structlog-Ausgaben farb- und formatierungsbereinigt in die Datei schreibt.
    """
    # 1. Temporaeren Logfile-Pfad festlegen
    temp_log_file = tmp_path / "test_app.log"

    # 2. Logger konfigurieren
    configure_logging(log_file_path=temp_log_file, backup_count=3)

    # 3. Test-Logmeldung absetzen
    logger = structlog.get_logger("test_logger")
    logger.info("Testnachricht fuer das rotierende Logfile", key_param="value_param")

    # 4. Verifizieren, dass die Datei existiert
    assert temp_log_file.exists()

    # 5. Inhalt verifizieren
    log_content = temp_log_file.read_text(encoding="utf-8")

    # Es sollte den Log-Text und die Key-Values enthalten
    assert "Testnachricht fuer das rotierende Logfile" in log_content
    assert "key_param=value_param" in log_content

    # Es darf KEINE ANSI-Farbcodes enthalten (wie z.B. ESC[32m oder \x1b)
    assert "\x1b" not in log_content

    # 6. Standard-Verhalten wiederherstellen (um andere Tests nicht zu beintraechtigen)
    configure_logging(log_file_path=Path(":memory:"), backup_count=1)
