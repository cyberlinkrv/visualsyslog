from __future__ import annotations

import argparse
import os
import webbrowser

import uvicorn

from visualsyslog.server import create_app


def main():
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
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.web_host}:{args.web_port}")).start()
    print(f"VisualSyslog: painel em http://{args.web_host}:{args.web_port} | Syslog em {app.state.bind_ip} UDP:{app.state.udp_port} / TCP:{app.state.tcp_port}")
    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level="info")


if __name__ == "__main__":
    main()
