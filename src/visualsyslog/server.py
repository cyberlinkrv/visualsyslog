from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_settings, save_settings, settings_file_path
from .store import LogStore, default_data_dir

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
        try:
            event_at = datetime.fromisoformat(m5424.group("timestamp").replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
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
    retention_days: int | None = Field(default=None, ge=1, le=36500)
    udp_port: int | None = Field(default=None, ge=1, le=65535)
    tcp_port: int | None = Field(default=None, ge=1, le=65535)


class DatabaseLocationUpdate(BaseModel):
    database_path: str = Field(min_length=1, max_length=1024)


def create_app(data_dir: Path | str | None = None, udp_host="0.0.0.0", udp_port=5514,
               tcp_host="0.0.0.0", tcp_port=5514, web_host="127.0.0.1", web_port=8080):
    udp_port = int(udp_port if udp_port is not None else os.getenv("VSYSLOG_UDP_PORT", "5514"))
    tcp_port = int(tcp_port if tcp_port is not None else os.getenv("VSYSLOG_TCP_PORT", "5514"))
    config_file = settings_file_path(data_dir)
    saved = load_settings(config_file)
    default_db = Path(data_dir).expanduser() / "visualsyslog.db" if data_dir else default_data_dir() / "visualsyslog.db"
    requested_db = Path(saved.get("database_path", default_db)).expanduser()
    storage_warning = None
    try:
        if saved.get("database_path") and not requested_db.exists():
            raise FileNotFoundError(f"O arquivo configurado não foi encontrado: {requested_db}")
        store = LogStore(requested_db, settings_file=config_file)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        fallback_dir = Path(data_dir).expanduser() if data_dir else default_data_dir()
        fallback_db = fallback_dir / "visualsyslog-recovery.db"
        store = LogStore(fallback_db, settings_file=config_file)
        storage_warning = (f"O banco configurado não está acessível ({requested_db}). A coleta foi pausada. "
                           f"Aponte para um banco disponível nas Configurações. Detalhe: {exc}")

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if app.state.storage_warning is None:
                    store.insert(parse_syslog(data.decode("utf-8", errors="replace"), str(addr[0]), "UDP"))

        app.state.udp_transport, _ = await loop.create_datagram_endpoint(
            Receiver, local_addr=(udp_host, app.state.udp_port))
        app.state.tcp_server = await asyncio.start_server(tcp_client, tcp_host, app.state.tcp_port)

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

    app = FastAPI(title="VisualSyslog", version="2.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.config_file = config_file
    app.state.udp_host = udp_host
    app.state.tcp_host = tcp_host
    app.state.web_host = web_host
    app.state.web_port = web_port
    app.state.udp_port = int(saved.get("udp_port", udp_port))
    app.state.tcp_port = int(saved.get("tcp_port", tcp_port))
    app.state.storage_warning = storage_warning
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    async def tcp_client(reader, writer):
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        try:
            while line := await reader.readline():
                raw = line.decode("utf-8", errors="replace")
                if raw.strip() and app.state.storage_warning is None:
                    store.insert(parse_syslog(raw, str(peer[0]), "TCP"))
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

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
        return {**store.stats(), "storage_warning": app.state.storage_warning,
                "listeners": {"udp": f"{udp_host}:{app.state.udp_port}",
                              "tcp": f"{tcp_host}:{app.state.tcp_port}"}}

    @app.get("/api/settings")
    async def get_settings():
        return {"retention_days": store.retention_days(), "database_path": str(store.path),
                "configured_database_path": saved.get("database_path", str(store.path)),
                "udp_port": app.state.udp_port, "tcp_port": app.state.tcp_port,
                "storage_warning": app.state.storage_warning}

    @app.put("/api/settings")
    async def set_settings(payload: SettingsUpdate):
        new_udp = payload.udp_port if payload.udp_port is not None else app.state.udp_port
        new_tcp = payload.tcp_port if payload.tcp_port is not None else app.state.tcp_port
        changed_udp = new_udp != app.state.udp_port
        changed_tcp = new_tcp != app.state.tcp_port
        new_udp_transport = None
        new_tcp_server = None
        loop = asyncio.get_running_loop()
        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if app.state.storage_warning is None:
                    store.insert(parse_syslog(data.decode("utf-8", errors="replace"), str(addr[0]), "UDP"))
        try:
            if changed_udp:
                new_udp_transport, _ = await loop.create_datagram_endpoint(
                    Receiver, local_addr=(udp_host, new_udp))
            if changed_tcp:
                new_tcp_server = await asyncio.start_server(tcp_client, tcp_host, new_tcp)
            save_settings(config_file, {"udp_port": new_udp, "tcp_port": new_tcp})
        except (OSError, RuntimeError) as exc:
            if new_udp_transport:
                new_udp_transport.close()
            if new_tcp_server:
                new_tcp_server.close()
                await new_tcp_server.wait_closed()
            raise HTTPException(status_code=400, detail=f"Não foi possível aplicar as portas: {exc}") from exc

        if changed_udp:
            app.state.udp_transport.close()
            app.state.udp_transport = new_udp_transport
            app.state.udp_port = new_udp
        if changed_tcp:
            old_tcp = app.state.tcp_server
            old_tcp.close()
            await old_tcp.wait_closed()
            app.state.tcp_server = new_tcp_server
            app.state.tcp_port = new_tcp
        if payload.retention_days is not None:
            store.set_retention_days(payload.retention_days)
        return await get_settings()

    @app.put("/api/settings/database")
    async def move_database(payload: DatabaseLocationUpdate):
        target = Path(payload.database_path).expanduser()
        if not target.is_absolute():
            raise HTTPException(status_code=400, detail="Informe o caminho completo do arquivo .db.")
        try:
            old_path, new_path = await asyncio.to_thread(store.move_to, target)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Não foi possível mover o banco: {exc}") from exc
        app.state.storage_warning = None
        saved.update({"database_path": str(new_path)})
        return {"database_path": str(new_path), "previous_database_path": str(old_path),
                "retention_days": store.retention_days(),
                "message": "Banco copiado e ativado. O arquivo original foi mantido como cópia de segurança."}

    return app
