"""Offline SQLite B-tree page attribution without the optional dbstat extension."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


class SqliteAttributionError(RuntimeError):
    """A snapshot is unsafe, malformed, or has conflicting page ownership."""


@dataclass(frozen=True, slots=True)
class SqliteObjectSize:
    name: str
    object_type: str
    table_name: str
    root_page: int
    entries: int
    pages: int
    allocated_bytes: int

    @property
    def average_bytes_per_entry(self) -> float | None:
        return None if self.entries == 0 else self.allocated_bytes / self.entries


@dataclass(frozen=True, slots=True)
class SqliteAttribution:
    path: Path
    page_size: int
    page_count: int
    total_bytes: int
    objects: tuple[SqliteObjectSize, ...]
    unattributed_pages: int


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(9):
        if offset + index >= len(data):
            raise SqliteAttributionError("truncated SQLite varint")
        byte = data[offset + index]
        if index == 8:
            return (value << 8) | byte, 9
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, index + 1
    raise AssertionError("unreachable")


def _local_payload(payload: int, usable: int, *, table_leaf: bool) -> int:
    minimum = ((usable - 12) * 32 // 255) - 23
    maximum = usable - 35 if table_leaf else ((usable - 12) * 64 // 255) - 23
    if payload <= maximum:
        return payload
    candidate = minimum + ((payload - minimum) % (usable - 4))
    return candidate if candidate <= maximum else minimum


class _SnapshotPages:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise SqliteAttributionError("SQLite snapshot does not exist")
        wal = path.with_name(f"{path.name}-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise SqliteAttributionError("SQLite attribution requires a fixed WAL-free snapshot")
        self.path = path
        self._handle = path.open("rb")
        header = self._handle.read(100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
            raise SqliteAttributionError("invalid SQLite snapshot header")
        encoded_page_size = int.from_bytes(header[16:18], "big")
        self.page_size = 65_536 if encoded_page_size == 1 else encoded_page_size
        self.reserved = header[20]
        self.usable = self.page_size - self.reserved
        self.page_count = int.from_bytes(header[28:32], "big")
        if self.page_size < 512 or self.page_count < 1:
            raise SqliteAttributionError("invalid SQLite page geometry")
        if path.stat().st_size < self.page_size * self.page_count:
            raise SqliteAttributionError("truncated SQLite snapshot")

    def close(self) -> None:
        self._handle.close()

    def page(self, number: int) -> bytes:
        if not 1 <= number <= self.page_count:
            raise SqliteAttributionError("SQLite page reference is out of range")
        start = (number - 1) * self.page_size
        self._handle.seek(start)
        value = self._handle.read(self.page_size)
        if len(value) != self.page_size:
            raise SqliteAttributionError("truncated SQLite page")
        return value

    def overflow_pages(self, first: int) -> tuple[int, ...]:
        pages: list[int] = []
        observed: set[int] = set()
        current = first
        while current:
            if current in observed:
                raise SqliteAttributionError("cyclic SQLite overflow chain")
            observed.add(current)
            pages.append(current)
            current = int.from_bytes(self.page(current)[:4], "big")
        return tuple(pages)

    def tree(self, root_page: int, ownership: bytearray, owner_id: int) -> tuple[int, int]:
        page_count = 0
        entries = 0
        stack = [root_page]

        def claim(number: int) -> None:
            nonlocal page_count
            if not 1 <= number <= self.page_count:
                raise SqliteAttributionError("SQLite page reference is out of range")
            if ownership[number] != 0:
                raise SqliteAttributionError("cyclic or shared SQLite page ownership")
            ownership[number] = owner_id
            page_count += 1

        while stack:
            number = stack.pop()
            claim(number)
            page = self.page(number)
            header = 100 if number == 1 else 0
            kind = page[header]
            if kind not in {0x02, 0x05, 0x0A, 0x0D}:
                raise SqliteAttributionError("unsupported SQLite B-tree page type")
            interior = kind in {0x02, 0x05}
            index_tree = kind in {0x02, 0x0A}
            cell_count = int.from_bytes(page[header + 3 : header + 5], "big")
            pointer_start = header + (12 if interior else 8)
            if index_tree or not interior:
                entries += cell_count
            if interior:
                stack.append(int.from_bytes(page[header + 8 : header + 12], "big"))
            for index in range(cell_count):
                pointer_offset = pointer_start + index * 2
                cell = int.from_bytes(page[pointer_offset : pointer_offset + 2], "big")
                if not 0 < cell < self.page_size:
                    raise SqliteAttributionError("invalid SQLite cell pointer")
                cursor = cell
                if interior:
                    stack.append(int.from_bytes(page[cursor : cursor + 4], "big"))
                    cursor += 4
                if kind == 0x05:
                    continue
                payload, payload_varint = _varint(page, cursor)
                cursor += payload_varint
                if kind == 0x0D:
                    _, rowid_varint = _varint(page, cursor)
                    cursor += rowid_varint
                local = _local_payload(payload, self.usable, table_leaf=kind == 0x0D)
                if payload > local:
                    overflow_pointer = cursor + local
                    if overflow_pointer + 4 > len(page):
                        raise SqliteAttributionError("truncated SQLite overflow pointer")
                    overflow = int.from_bytes(page[overflow_pointer : overflow_pointer + 4], "big")
                    for overflow_page in self.overflow_pages(overflow):
                        claim(overflow_page)
        return page_count, entries


def attribute_sqlite_snapshot(path: Path) -> SqliteAttribution:
    """Attribute every table/index B-tree page in a fixed immutable snapshot."""

    resolved = path.resolve()
    pages = _SnapshotPages(resolved)
    try:
        uri = f"file:{resolved.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            schema = tuple(
                connection.execute(
                    """SELECT name,type,tbl_name,rootpage FROM sqlite_schema
                    WHERE rootpage>0 ORDER BY rootpage,name"""
                )
            )
        finally:
            connection.close()
        if len(schema) > 255:
            raise SqliteAttributionError("too many SQLite objects for bounded attribution")
        ownership = bytearray(pages.page_count + 1)
        owned_pages = 0
        objects: list[SqliteObjectSize] = []
        for owner_id, (name, object_type, table_name, root_page) in enumerate(schema, 1):
            object_pages, entries = pages.tree(int(root_page), ownership, owner_id)
            owned_pages += object_pages
            objects.append(
                SqliteObjectSize(
                    name=str(name),
                    object_type=str(object_type),
                    table_name=str(table_name),
                    root_page=int(root_page),
                    entries=entries,
                    pages=object_pages,
                    allocated_bytes=object_pages * pages.page_size,
                )
            )
        unattributed = pages.page_count - owned_pages
        if unattributed < 0:
            raise SqliteAttributionError("SQLite page attribution exceeds page count")
        return SqliteAttribution(
            path=resolved,
            page_size=pages.page_size,
            page_count=pages.page_count,
            total_bytes=pages.page_count * pages.page_size,
            objects=tuple(sorted(objects, key=lambda item: item.allocated_bytes, reverse=True)),
            unattributed_pages=unattributed,
        )
    finally:
        pages.close()
