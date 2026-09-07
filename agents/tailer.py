"""增量 JSONL tailer：只读追加，轮询 stat，读增量，容忍最后一行残缺。"""
import os
from collections import deque


class FileTailer:
    MAX_CHUNK = 4 * 1024 * 1024  # 单次最多读 4MB 增量，防爆炸
    MAX_LINE = 1024 * 1024        # 单行上限；异常长行不应占满常驻内存

    def __init__(self, path: str):
        self.path = path
        self.pos = 0
        self._buf = b""
        self._identity = None       # (st_dev, st_ino)，识别原地替换/轮换
        self._prefix = b""          # detects overwrite/replace on filesystems without inode
        self._dropping = False      # 正在跳过超长行直到下一个换行

    def poll(self) -> deque[str]:
        """返回新完成的行（str，UTF-8 容错）。文件截断/轮换时自动从头开始。"""
        lines: deque[str] = deque()
        try:
            st = os.stat(self.path)
        except OSError:
            return lines
        size = st.st_size
        identity = (getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
        replaced = self._identity is not None and identity != self._identity
        # Windows and some network filesystems report a constant/zero inode.
        # A short stable prefix lets us distinguish an overwrite from append
        # without retaining the file or reading the whole transcript.
        if self._identity is not None and not replaced:
            try:
                with open(self.path, "rb") as prefix_file:
                    prefix = prefix_file.read(256)
                # A short file grows into a longer prefix during normal
                # append.  Compare only the bytes that existed on the prior
                # poll; comparing the whole prefix would rewind and duplicate
                # every record whenever such a file grew past its old size.
                replaced = bool(self._prefix and
                                prefix[:len(self._prefix)] != self._prefix)
            except OSError:
                prefix = self._prefix
        else:
            try:
                with open(self.path, "rb") as prefix_file:
                    prefix = prefix_file.read(256)
            except OSError:
                prefix = b""
        if replaced:
            # 文件可能被原子替换但新文件更大，不能只依赖 size < pos。
            self.pos = 0
            self._buf = b""
            self._dropping = False
        self._identity = identity
        self._prefix = prefix
        if size < self.pos:      # 文件被截断/重建
            self.pos = 0
            self._buf = b""
            self._dropping = False
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
        # 一次 split 避免对同一缓冲区逐行 partition 造成重复扫描；保留
        # 最后一段残行，下次追加后再交付。
        parts = self._buf.split(b"\n")
        self._buf = parts.pop()
        for raw in parts:
            if self._dropping:
                self._dropping = False
                continue
            if len(raw) > self.MAX_LINE:
                # 这行已经完整读到，丢掉它但继续处理后续行。
                continue
            line = raw.decode("utf-8", errors="replace").strip("\r")
            if line:
                lines.append(line)

        # 没有换行的超长残行只保留一个布尔标记，避免不断扩张 bytes 缓冲。
        if len(self._buf) > self.MAX_LINE:
            self._buf = b""
            self._dropping = True
        return lines

    def close(self):
        self.pos = 0
        self._buf = b""
        self._identity = None
        self._prefix = b""
        self._dropping = False
