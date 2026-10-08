from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from sim import server


def test_recording_export_uses_generated_files_and_fixed_ffmpeg_settings(tmp_path, monkeypatch):
    commands = []

    def fake_ffmpeg(args, **kwargs):
        commands.append((args, kwargs))
        Path(args[-1]).write_bytes(b'encoded')
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(server, 'RECORDINGS_DIR', tmp_path)
    monkeypatch.setattr(server.subprocess, 'run', fake_ffmpeg)
    response = TestClient(server.app).post(
        '/api/twin/recording',
        content=b'webm-data',
        headers={'content-type': 'video/webm;codecs=vp9'},
    )

    assert response.status_code == 200
    result = response.json()
    assert result['gif_url'].startswith('/api/twin/recordings/color-twin-')
    assert result['mp4_url'].endswith('.mp4')
    assert (tmp_path / result['gif_filename']).read_bytes() == b'encoded'
    assert (tmp_path / result['mp4_filename']).read_bytes() == b'encoded'
    assert len(commands) == 2
    assert all(command[0][:5] == ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] for command in commands)
    assert 'scale=720:-2:flags=lanczos,fps=10' in commands[0][0][commands[0][0].index('-vf') + 1]
    assert 'palettegen' in commands[0][0][commands[0][0].index('-vf') + 1]
    assert 'scale=960:-2:flags=lanczos,fps=12' in commands[1][0][commands[1][0].index('-vf') + 1]
    assert commands[1][0][commands[1][0].index('-crf') + 1] == '18'
    assert not list(tmp_path.glob('*.webm'))


def test_recording_export_rejects_wrong_type_and_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'RECORDINGS_DIR', tmp_path)
    monkeypatch.setattr(server, 'MAX_RECORDING_BYTES', 4)
    client = TestClient(server.app)

    wrong_type = client.post(
        '/api/twin/recording',
        content=b'video',
        headers={'content-type': 'video/mp4'},
    )
    assert wrong_type.status_code == 415

    oversize = client.post(
        '/api/twin/recording',
        content=b'12345',
        headers={'content-type': 'video/webm'},
    )
    assert oversize.status_code == 413
    assert not list(tmp_path.iterdir())


def test_recording_export_rejects_empty_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'RECORDINGS_DIR', tmp_path)
    response = TestClient(server.app).post(
        '/api/twin/recording',
        content=b'',
        headers={'content-type': 'video/webm'},
    )
    assert response.status_code == 422
    assert not list(tmp_path.iterdir())
