from __future__ import annotations

import ipaddress
import socket


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return ip.version == 4 and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)
    except ValueError:
        return False


def get_default_bind_ip() -> str:
    """Return the IPv4 source address selected by the OS default route without sending data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect only asks the local routing table which source IP it would use.
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
        if _usable(address):
            return address
    except OSError:
        pass
    finally:
        sock.close()
    try:
        candidates = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        for candidate in candidates:
            address = candidate[4][0]
            if _usable(address):
                return address
    except OSError:
        pass
    return "127.0.0.1"


def is_ip_assigned(address: str) -> bool:
    """Check whether an IP is currently bindable on this host, including wildcard addresses."""
    ip = ipaddress.ip_address(address)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()
