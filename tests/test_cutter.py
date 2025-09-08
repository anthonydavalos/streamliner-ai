# tests/test_cutter.py

import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

from streamliner.cutter import VideoCutter


@pytest.mark.asyncio
async def test_cut_clip_success():
    """
    Verifica que VideoCutter llama a ffmpeg con los argumentos correctos.
    """
    # 1. Preparación (Arrange)
    cutter = VideoCutter()
    input_path = Path("/tmp/source.mp4")
    output_path = Path("/tmp/output.mp4")
    start = 10.5
    end = 25.0

    # Mock del subproceso de asyncio
    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"stdout", b"stderr")

    # 2. Acción (Act)
    # Usamos 'patch' para reemplazar la función real de asyncio con nuestro mock
    with patch(
        "asyncio.create_subprocess_exec", return_value=mock_process
    ) as mock_exec:
        result_path = await cutter.cut_clip(input_path, output_path, start, end)

    # 3. Aserción (Assert)
    # Verificamos que se llamó a la creación del subproceso
    mock_exec.assert_called_once()

    # Verificamos que los argumentos del comando ffmpeg son los esperados
    expected_args = [
        "ffmpeg",
        "-y",
        "-ss",
        "10.5",
        "-i",
        str(input_path),
        "-to",
        "25.0",
        "-c",
        "0",
        str(output_path),
    ]
    # Comparamos los argumentos con los que se usaron en la llamada real
    mock_exec.assert_called_with(
        *expected_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # Verificamos que la función devolvió la ruta de salida correcta
    assert result_path == str(output_path)
