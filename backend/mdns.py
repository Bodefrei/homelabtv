import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_homelabtv._tcp.local."
SERVICE_NAME = f"HomelabTV Backend.{SERVICE_TYPE}"
SERVICE_HOST = "homelabtv.local."
SERVICE_PORT = 30060


def _lan_ipv4_address() -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("224.0.0.251", 5353))
        address = probe.getsockname()[0]
    if address.startswith("127.") or address == "0.0.0.0":
        raise RuntimeError("Could not determine the server's LAN IPv4 address for mDNS")
    return socket.inet_aton(address)


async def register_mdns_service() -> AsyncZeroconf:
    zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
    service = ServiceInfo(
        SERVICE_TYPE,
        SERVICE_NAME,
        addresses=[_lan_ipv4_address()],
        port=SERVICE_PORT,
        properties={"path": "/", "version": "1"},
        server=SERVICE_HOST,
    )
    try:
        await zeroconf.async_register_service(service)
    except Exception:
        await zeroconf.async_close()
        raise
    logger.info("Advertising HomelabTV at %s:%d via mDNS", SERVICE_HOST, SERVICE_PORT)
    return zeroconf
