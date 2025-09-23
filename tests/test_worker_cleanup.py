import asyncio
from collections import deque
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_add_chunk_deletes_orphan(tmp_path, monkeypatch):
    # Importar dentro del test para asegurar que el entorno esté preparado
    from src.streamliner.worker import ProcessingWorker

    # Crear archivos temporales que simulan chunks
    a = tmp_path / "chunk_a.mp4"
    b = tmp_path / "chunk_b.mp4"
    c = tmp_path / "chunk_c.mp4"
    for p in (a, b, c):
        p.write_bytes(b"0" * 10)

    # Stub de config mínimo
    class RT:
        chunk_duration_seconds = 1
        highlight_buffer_size = 2
        min_chunks_for_detection = 99  # forzamos a que no dispare análisis real

    class Cfg:
        real_time_processing = RT()
        transcription = type(
            "T", (), {"whisper_model": "small", "device": "cpu", "compute_type": "int8"}
        )
        detection = type("D", (), {})
        paths = type(
            "P", (), {"transcriber_models_dir": Path("data/transcriber_models")}
        )
        publishing = None

    # Evitar inicialización pesada del worker parcheando __init__
    def fake_init(self, config, streamer, stream_session_dir, dry_run=False):
        self.config = config
        self.streamer = streamer
        self.stream_session_dir = stream_session_dir
        self.dry_run = dry_run
        self.highlight_buffer = deque(
            maxlen=config.real_time_processing.highlight_buffer_size
        )
        self.clip_processing_lock = asyncio.Lock()
        self.current_stream_time_offset = 0
        self._analysis_task = None

    monkeypatch.setattr(ProcessingWorker, "__init__", fake_init)

    # No hacer trabajo real en el análisis
    async def noop(self):
        self._analysis_task = None

    monkeypatch.setattr(ProcessingWorker, "_process_highlights_from_buffer", noop)

    # Parchear _safe_delete para eliminar en disco sin esperas
    deleted = []

    async def fast_delete(self, path, max_retries=1, retry_delay=0):
        deleted.append(path.name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    monkeypatch.setattr(ProcessingWorker, "_safe_delete", fast_delete)

    worker = ProcessingWorker(Cfg(), "tester", tmp_path, dry_run=True)

    # Añadir dos chunks (no debe borrar nada todavía)
    await worker.add_chunk_for_processing(a)
    await worker.add_chunk_for_processing(b)
    assert a.exists() and b.exists()
    assert list(worker.highlight_buffer) == [a, b]

    # Añadir tercer chunk: el deque expulsará 'a'; debe eliminarse del disco
    await worker.add_chunk_for_processing(c)

    assert not a.exists(), "El primer chunk expulsado no fue eliminado del disco"
    assert b.exists() and c.exists(), "Chunks actuales no deben eliminarse"
    assert set(deleted) >= {"chunk_a.mp4"}
    assert list(worker.highlight_buffer) == [b, c]
