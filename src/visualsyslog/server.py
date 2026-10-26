from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import LogStore

PRI = re.compile(r"^<(?P<pri>\d{1,3})>(?P<body>.*)$", re.S)
RFC5424 = re.compile(r"^\d+\s+(?P<timestamp>\S+)\s+(?P<host>\S+)\s+(?P<app>\S+)\s+\S+\s+\S+\s+(?:-|\[.*?\])\s*(?P<msg>.*)$", re.S)
RFC3164 = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+(?P<host>\S+)\s+(?:(?P<app>[^\[:]+)(?:\[\d+\])?:\s*)?(?P<msg>.*)$", re.S)


def parse_syslog(raw: str, source_ip: str, protocol: str) -> dict:
    text = raw.strip("\x00\r\n ")
    match = PRI.match(text)
    pri = int(match.group("pri")) if match else None
    body = match.group("body") if match else text
    host = app = event_at = None
    m5424 = RFC5424.match(body)
    if m5424:
        host, app, message = m5424.group("host"), m5424.group("app"), m5424.group("msg")
        stamp = m5424.group("timestamp")
        try:
            event_at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            event_at = None
    else:
        m3164 = RFC3164.match(body)
        if m3164:
            host, app, message = m3164.group("host"), m3164.group("app"), m3164.group("msg")
        else:
            message = body
    return {"facility": pri // 8 if pri is not None else None,
            "severity": pri % 8 if pri is not None else None,
            "hostname": host, "app_name": app, "event_at": event_at,
            "source_ip": source_ip, "protocol": protocol,
            "message": message, "raw": text}


class SettingsUpdate(BaseModel):
    retention_days: int = Field(ge=1, le=36500)


def create_app(data_dir: Path | str | None = None, udp_host="0.0.0.0", udp_port=5514,
               tcp_host="0.0.0.0", tcp_port=5514, web_host="127.0.0.1", web_port=8080):
    store = LogStore(Path(data_dir) / "visualsyslog.db" if data_dir else None)
    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                store.insert(parse_syslog(data.decode("utf-8", errors="replace"), str(addr[0]), "UDP"))
        app.state.udp_transport, _ = await loop.create_datagram_endpoint(
            Receiver, local_addr=(udp_host, udp_port))
        app.state.tcp_server = await asyncio.start_server(tcp_client, tcp_host, tcp_port)
        async def maintenance():
            while True:
                await asyncio.to_thread(store.purge_expired)
                await asyncio.sleep(3600)
        app.state.maintenance_task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            app.state.udp_transport.close()
            app.state.tcp_server.close()
            await app.state.tcp_server.wait_closed()
            app.state.maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.maintenance_task
            store.close()

    app = FastAPI(title="VisualSyslog", version="2.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.config = {"udp_host": udp_host, "udp_port": udp_port, "tcp_host": tcp_host, "tcp_port": tcp_port,
                        "web_host": web_host, "web_port": web_port}
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def home():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/messages")
    async def messages(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                       search: str = "", severity: int | None = Query(None, ge=0, le=7)):
        rows, total = store.list_messages(limit, offset, search, severity)
        return {"items": rows, "total": total}

    @app.get("/api/stats")
    async def stats():
        return {**store.stats(), "listeners": {"udp": f"{udp_host}:{udp_port}", "tcp": f"{tcp_host}:{tcp_port}"}}

    @app.get("/api/settings")
    async def get_settings():
        return {"retention_days": store.retention_days()}

    @app.put("/api/settings")
    async def set_settings(payload: SettingsUpdate):
        store.set_retention_days(payload.retention_days)
        return await get_settings()

    async def tcp_client(reader, writer):
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        try:
            while line := await reader.readline():
                raw = line.decode("utf-8", errors="replace")
                if raw.strip():
                    store.insert(parse_syslog(raw, str(peer[0]), "TCP"))
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    return app
