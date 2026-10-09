"""信号跳变追踪（现场排故 / 离线复现）。

输出两种格式：

* **CSV（长表）** —— ``timestamp_s,interface,source,state,name,value``。
  对任意信号集合都成立，容易 grep、也容易用 pandas 透视成波形；
* **JSONL** —— 每行一个 JSON 对象，适合接入日志平台。

只有**发生变化的信号**才会被写出（内置变更检测），所以长时间运行的追踪文件
不会因为每拍重复写入而膨胀。
"""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from typing import IO, Dict, Mapping, Optional, Tuple

__all__ = ["TraceRecorder"]

_CSV_HEADER = ("timestamp_s", "interface", "source", "state", "name", "value")


class TraceRecorder:
    """把信号变化写入 CSV 或 JSONL。"""

    def __init__(
        self,
        path: str,
        *,
        fmt: str = "csv",
        flush_every: int = 1,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.fmt = fmt.strip().lower()
        if self.fmt not in ("csv", "jsonl"):
            raise ValueError(f"不支持的追踪格式: {fmt!r}（可选: csv / jsonl）")
        self.flush_every = max(1, int(flush_every))
        self.enabled = enabled
        self._lock = threading.RLock()
        self._last: Dict[Tuple[str, str, str], bool] = {}
        self._pending = 0
        self._handle: Optional[IO[str]] = None
        self._writer: Optional[csv.writer] = None
        self._rows_written = 0
        if self.enabled:
            self._open()

    # ------------------------------------------------------------------ 生命周期
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="")
        if self.fmt == "csv":
            self._writer = csv.writer(self._handle)
            self._writer.writerow(_CSV_HEADER)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.flush()
                    self._handle.close()
                finally:
                    self._handle = None
                    self._writer = None

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ 写入
    def record(
        self,
        *,
        timestamp: float,
        interface_id: str,
        state: str,
        signals: Mapping[str, bool],
        source: str = "output",
        channels: Optional[Mapping[str, bool]] = None,
        force: bool = False,
    ) -> int:
        """记录一组信号；只写发生变化（或 ``force=True``）的那些。返回写入条数。"""

        if not self.enabled or self._handle is None:
            return 0
        written = 0
        with self._lock:
            for name, value in signals.items():
                key = (interface_id, source, name)
                value = bool(value)
                if not force and self._last.get(key) == value:
                    continue
                self._last[key] = value
                self._write_row(timestamp, interface_id, source, state, name, value)
                written += 1
            if channels and source == "channel":
                for name, level in channels.items():
                    key = (interface_id, "channel", name)
                    value = bool(level)
                    if not force and self._last.get(key) == value:
                        continue
                    self._last[key] = value
                    self._write_row(timestamp, interface_id, "channel", state, name, value)
                    written += 1
        return written

    def _write_row(
        self,
        timestamp: float,
        interface_id: str,
        source: str,
        state: str,
        name: str,
        value: bool,
    ) -> None:
        assert self._handle is not None
        if self.fmt == "csv":
            assert self._writer is not None
            self._writer.writerow(
                (f"{timestamp:.6f}", interface_id, source, state, name, int(value))
            )
        else:
            self._handle.write(
                json.dumps(
                    {
                        "t": round(timestamp, 6),
                        "interface": interface_id,
                        "source": source,
                        "state": state,
                        "name": name,
                        "value": int(value),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self._rows_written += 1
        self._pending += 1
        if self._pending >= self.flush_every:
            self._handle.flush()
            self._pending = 0

    # ------------------------------------------------------------------ 统计
    @property
    def rows_written(self) -> int:
        return self._rows_written

    def reset_baseline(self) -> None:
        """清空变更基线：下一次 :meth:`record` 会把全部信号当作变化写出。"""

        with self._lock:
            self._last.clear()
