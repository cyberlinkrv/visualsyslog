from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from visualsyslog.server import create_app


def _configure_hidden_output() -> Path | None:
    """Give windowed Windows builds a persistent log instead of discarding diagnostics."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VisualSyslog" / "logs"
    log_path = base / "visualsyslog-startup.log"
    try:
        base.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8", buffering=1)
        log_path = None
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return log_path


def _panel_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _open_browser_when_ready(host: str, port: int, timeout: float = 60.0) -> None:
    """Wait until the local dashboard answers before launching the browser."""
    url = _panel_url(host, port)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    print(f"Painel não respondeu em {timeout:.0f}s: {url}", file=sys.stderr)


def _show_startup_error(error: Exception, log_path: Path | None) -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        details = f"VisualSyslog não conseguiu iniciar.\n\n{error}"
        if log_path:
            details += f"\n\nDetalhes: {log_path}"
        ctypes.windll.user32.MessageBoxW(None, details, "VisualSyslog — falha ao iniciar", 0x10)
    except Exception:
        pass


def main():
    log_path = _configure_hidden_output()
    try:
        _run(log_path)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        _show_startup_error(error, log_path)
        raise


def _run(log_path: Path | None = None):
    p = argparse.ArgumentParser(description="Servidor Syslog VisualSyslog")
    p.add_argument("--udp-port", type=int, default=None)
    p.add_argument("--tcp-port", type=int, default=None)
    p.add_argument("--web-host", default=os.getenv("VSYSLOG_WEB_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.getenv("VSYSLOG_WEB_PORT", "8080")))
    p.add_argument("--syslog-host", default=os.getenv("VSYSLOG_HOST"))
    p.add_argument("--data-dir", default=os.getenv("VISUALSYSLOG_DATA_DIR"))
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    app = create_app(args.data_dir, args.syslog_host, args.udp_port, args.syslog_host, args.tcp_port,
                     args.web_host, args.web_port)
    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready,
                         args=(args.web_host, args.web_port), daemon=True).start()
    print(f"VisualSyslog: painel em {_panel_url(args.web_host, args.web_port)} | Syslog em {app.state.bind_ip} UDP:{app.state.udp_port} / TCP:{app.state.tcp_port}")
    if log_path:
        print(f"Log de inicialização: {log_path}")
    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level="info")


if __name__ == "__main__":
    main()
