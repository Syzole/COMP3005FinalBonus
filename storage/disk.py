"""Disk manager: fixed 4096-byte pages, counted reads/writes."""

from __future__ import annotations

from pathlib import Path

PAGE_SIZE = 4096