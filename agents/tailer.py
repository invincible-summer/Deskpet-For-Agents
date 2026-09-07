"""增量 JSONL tailer：只读追加，轮询 stat，读增量，容忍最后一行残缺。"""
import os
from collections import deque


class FileTailer:
    MAX_CHUNK = 4 * 1024 * 1024  # 单次最多读 4MB 增量，防爆炸

    def __init__(self, path: str):
        self.path = path
        self.pos = 0
        self._buf = b""

    def poll(self) -> deque[str]:
        """返回新完成的行（str，UTF-8 容错）。文件截断/轮换时自动从头开始。"""
        lines: deque[str] = deque()
        try:
            st = os.stat(self.path)
        except OSError:
            return lines
        size = st.st_size
        if size < self.pos:      # 文件被截断/重建
            self.pos = 0
            self._buf = b""
        if size == self.pos:
            return lines
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(size - self.pos, self.MAX_CHUNK))
                self.pos = f.tell()
        except OSError:
            return lines
        self._buf += data
        while b"\n" in self._buf:
            raw, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            line = raw.decode("utf-8", errors="replace").strip("\r")
            if line:
                lines.append(line)
        # 防御：残行缓冲过大（说明不是行式文件），丢弃
        if len(self._buf) > 1024 * 1024:
            self._buf = b""
        return lines

    def close(self):
        self._buf = b""
