# tests/test_cutter.py

import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch
import shutil  # Necesario para la limpieza

from streamliner.cutter import VideoCutter


@pytest.mark.asyncio
async def test_cut_clip_success(tmp_path: Path):
    """
    Verifica que VideoCutter llama a ffmpeg con los argumentos correctos
    para la recodificación del clip.
    """
    # 1. Preparación
    mock_clips_output_dir = tmp_path / "clips_output"
    mock_clips_output_dir.mkdir(
        parents=True, exist_ok=True
    )  # Asegurarse de que el directorio existe

    cutter = VideoCutter(clips_output_dir=mock_clips_output_dir)

    input_path = Path("/tmp/source.mp4")
    start = 10.5
    end = 25.0
    output_filename = "output.mp4"
    final_output_path = mock_clips_output_dir / output_filename

    # Calcular la duración, como lo haría ffmpeg con -t
    duration = end - start

    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"stdout", b"stderr")

    # 2. Acción
    with patch(
        "asyncio.create_subprocess_exec", return_value=mock_process
    ) as mock_exec:
        result_path = await cutter.cut_clip(input_path, start, end, output_filename)

    # 3. Aserción
    mock_exec.assert_called_once()

    # ¡ACTUALIZACIÓN CRÍTICA AQUÍ para que coincida con el comportamiento REAL de VideoCutter!
    expected_args = [
        "ffmpeg",
        "-y",
        "-i",  # <- Orden cambiado: -i va antes de -ss
        str(input_path),
        "-ss",
        str(start),
        "-t",  # <- Usar -t para duración
        str(duration),  # <- Duración calculada
        "-c:v",
        "copy",  # <- Añadir argumentos de códec
        "-c:a",
        "copy",  # <- Añadir argumentos de códec
        str(final_output_path),
    ]

    mock_exec.assert_called_with(
        *expected_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert result_path == final_output_path

    shutil.rmtree(mock_clips_output_dir)
