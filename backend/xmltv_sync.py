import gzip
import io
import logging
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 180


def _timestamp(value: str | None) -> float | None:
    if not value or len(value) < 14:
        return None
    try:
        timestamp = datetime.strptime(
            f"{value[:14]} {value[14:].strip()}",
            "%Y%m%d%H%M%S %z",
        )
        return timestamp.timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def _prune_xmltv(xml_data: bytes, now: float, past_hours: int, future_hours: int) -> bytes:
    root = ET.fromstring(xml_data)
    if root.tag != "tv":
        raise ValueError("Downloaded XMLTV document has no <tv> root element")

    past_cutoff = now - past_hours * 60 * 60
    future_cutoff = now + future_hours * 60 * 60
    programs = root.findall("programme")
    if not programs:
        raise ValueError("Downloaded XMLTV document contains no programmes")

    retained_programs = set()
    for program in programs:
        start = _timestamp(program.get("start"))
        stop = _timestamp(program.get("stop"))
        if start is None or stop is None or not (stop <= past_cutoff or start >= future_cutoff):
            retained_programs.add(id(program))

    if not retained_programs:
        raise ValueError("Downloaded XMLTV document contains no programmes in the retention window")
    root[:] = [
        child
        for child in root
        if child.tag != "programme" or id(child) in retained_programs
    ]
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def download_xmltv(
    url: str,
    destination: str,
    past_hours: int,
    future_hours: int,
) -> int:
    if not url.startswith("https://"):
        raise ValueError("The XMLTV feed URL must use HTTPS")

    compressed = bytearray()
    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept-Encoding": "identity"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_raw():
                compressed.extend(chunk)
                if len(compressed) > MAX_COMPRESSED_BYTES:
                    raise ValueError("Compressed XMLTV feed exceeds the 64 MiB download limit")

    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source:
        xml_data = bytearray()
        while chunk := source.read(64 * 1024):
            xml_data.extend(chunk)
            if len(xml_data) > MAX_XML_BYTES:
                raise ValueError("Expanded XMLTV feed exceeds the 512 MiB size limit")

    cleaned_xml = _prune_xmltv(bytes(xml_data), time.time(), past_hours, future_hours)
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(cleaned_xml)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)

    logger.info("Updated XMLTV feed %s: wrote %d bytes to %s", url, len(cleaned_xml), output_path)
    return len(cleaned_xml)
