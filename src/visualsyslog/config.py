from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def settings_file_path(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser() / "settings.json"
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home())) / "VisualSyslog"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "visualsyslog"
    return root / "settings.json"


def load_settings(path: Path | str) -> dict:
    settings_path = Path(path)
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Não foi possível ler as configurações em {settings_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"O arquivo de configurações {settings_path} não contém um objeto JSON válido.")
    return data


def save_settings(path: Path | str, updates: dict) -> dict:
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data = load_settings(settings_path)
    data.update(updates)
    fd, temp_name = tempfile.mkstemp(prefix=".visualsyslog-settings-", suffix=".tmp", dir=settings_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, settings_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return data
