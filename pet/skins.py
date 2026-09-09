"""皮肤管理：发现、构建（转换）、缓存。

注意：不要在本模块顶层 import tools.convert（会连带加载 PIL）。
转换全部走独立子进程 python -m tools.convert，主进程保持低内存。
"""
import json
import os
import queue
import threading

STATES = ["walk", "attack", "die", "special", "sleep"]

# 程序化原创 fallback 皮肤（v4.2.3 §10.4）：公开源码包无用户版权素材
# 时仍首启可见。唯一常量，fresh config 与 desired_build_key 的最终
# fallback 都引用它，不再出现隐藏的 amiya 默认值。
# 必须定义在 `from .config import ...` 之前：pet.config 反向引用本常量
# 构造 DEFAULTS，导入顺序保证两个方向都无循环失败。
BUILTIN_SKIN = "builtin-cat"

# builtin-cat 每 state 的循环语义（与导入 manifest 一致）
_BUILTIN_LOOP = {"walk": True, "sleep": True, "attack": False,
                 "die": False, "special": False}

from .config import CACHE_DIR, PETS_DIR  # noqa: E402


def _scan_skins() -> dict[str, dict]:
    """磁盘扫描（只允许在 SkinCatalog.refresh_from_disk 内/测试调用）。"""
    out: dict[str, dict] = {
        BUILTIN_SKIN: {
            "name": BUILTIN_SKIN,
            "title": "DeskPet 原创小猫（内置）",
            "builtin": True,
        },
    }
    try:
        entries = sorted(os.listdir(PETS_DIR))
    except OSError:
        return out
    for name in entries:
        d = os.path.join(PETS_DIR, name)
        mf = os.path.join(d, "manifest.json")
        if not os.path.isdir(d):
            continue
        manifest = {}
        if os.path.isfile(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    manifest = json.load(f)
            except ValueError:
                manifest = {}
        else:
            missing = [s for s in STATES
                       if not any(os.path.isfile(os.path.join(d, s + ext))
                                  for ext in (".webm", ".mp4", ".gif"))]
            if missing:
                continue
            manifest = {"name": name, "title": name}
        manifest.setdefault("name", name)
        out[name] = manifest
    return out


class SkinCatalog:
    """进程内皮肤目录快照（v4.3 §9）。

    右键菜单 / Dashboard / load_skin 只读内存 snapshot，不再每次
    os.listdir + open(manifest)。revision 在每次 refresh_from_disk
    时 +1（UI 据此刷新 combobox values）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.revision = 0
        self._snapshot: dict[str, dict] = {}

    def snapshot(self) -> dict[str, dict]:
        """只读快照（调用方不得修改；空快照自动首次扫描）。"""
        with self._lock:
            if not self._snapshot:
                self._snapshot = _scan_skins()
                self.revision += 1
            return self._snapshot

    def refresh_from_disk(self) -> dict[str, dict]:
        """重新扫描（导入完成/外部变更后；非 Tk 线程调用）。"""
        with self._lock:
            self._snapshot = _scan_skins()
            self.revision += 1
            return self._snapshot


_catalog = SkinCatalog()


def list_skins() -> dict[str, dict]:
    """{皮肤名: manifest}（内存快照；注入虚拟 builtin-cat）。"""
    return _catalog.snapshot()


def refresh_skin_catalog() -> dict[str, dict]:
    """强制重扫磁盘（v4.3 §9 导入完成后调用）。"""
    return _catalog.refresh_from_disk()


def catalog_revision() -> int:
    return _catalog.revision


def cache_dir(skin: str, height: int) -> str:
    return os.path.join(CACHE_DIR, f"{skin}@{height}")


def built_gifs(skin: str, height: int) -> dict[str, str] | None:
    """已构建的 GIF 路径表；不完整返回 None。"""
    d = cache_dir(skin, height)
    out = {}
    for s in STATES:
        p = os.path.join(d, s + ".gif")
        if not os.path.isfile(p) or not os.path.isfile(p + ".json"):
            return None
        out[s] = p
    return out


def build_skin(skin: str, height: int, fps: int, log=None) -> dict[str, str]:
    """构建（或复用缓存）皮肤 GIF。耗时操作，勿在 UI 线程调用。

    v4.3 §6.6：转换只走独立子进程（numpy/scipy 内存随子进程退出
    释放）；子进程失败就是该 build 失败——绝不在 DeskPet 主进程
    重新 import 转换器（那会把 numpy/scipy 载入常驻 UI 进程）。
    """
    cached = built_gifs(skin, height)
    if cached:
        return cached
    if skin == BUILTIN_SKIN:
        # v4.2.3 §10.4：程序化 fallback 皮肤不进 ffmpeg/numpy/scipy
        # 子进程，直接用 pet.icon.draw_cat 经 Pillow 生成五个轻量
        # 单帧 GIF + meta JSON；产物只在 assets/cache/。
        return _build_builtin_skin(height, log)
    src = os.path.join(PETS_DIR, skin)
    out_dir = cache_dir(skin, height)
    if log:
        log(f"正在构建皮肤 {skin}（{height}px）…")
    if not _convert_in_subprocess(src, out_dir, height, fps, log):
        raise RuntimeError(
            f"皮肤 {skin} 构建失败：converter 子进程失败（继续使用原皮肤）")
    built = built_gifs(skin, height)
    if not built:
        raise RuntimeError(f"皮肤 {skin} 构建失败")
    if log:
        log(f"皮肤 {skin} 构建完成")
    return built


def _build_builtin_skin(height: int, log=None) -> dict[str, str]:
    """builtin-cat：draw_cat + Pillow 生成单帧 GIF 与 meta（§10.4）。"""
    from .icon import draw_cat

    height = max(96, min(960, int(height)))
    out_dir = cache_dir(BUILTIN_SKIN, height)
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for state in STATES:
        out_gif = os.path.join(out_dir, state + ".gif")
        if not (os.path.isfile(out_gif)
                and os.path.isfile(out_gif + ".json")):
            img = draw_cat(height).convert("P")
            img.save(out_gif, save_all=True, append_images=[],
                     duration=1000, loop=0)
            with open(out_gif + ".json", "w", encoding="utf-8") as f:
                json.dump({
                    "frames": 1, "width": img.width, "height": img.height,
                    "delay_ms": 1000, "loop": _BUILTIN_LOOP.get(state, True),
                }, f, ensure_ascii=False)
        paths[state] = out_gif
    if log:
        log(f"内置皮肤 {BUILTIN_SKIN}（{height}px）已生成")
    return paths


def _convert_in_subprocess(src: str, out_dir: str, height: int, fps: int,
                           log=None) -> bool:
    """python -m tools.convert 子进程转换。成功返回 True。

    v4.3 §6.6：失败不再回退进程内转换——直接报告 build error，
    主进程保持不加载 numpy/scipy。
    """
    import subprocess
    import sys
    exe = sys.executable
    # pythonw 没有 stdout，子进程里 print 会崩，优先用 python.exe
    if os.path.splitext(exe)[0].endswith("pythonw"):
        sibling = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(sibling):
            exe = sibling
    cmd = [exe, "-m", "tools.convert", src, out_dir, str(height), str(fps)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        if log:
            log(f"子进程转换异常: {e!r}")
        return False
    if r.returncode == 0:
        return True
    if log:
        err = r.stderr.decode("utf-8", errors="replace")[-400:]
        log(f"子进程转换失败(code={r.returncode}): {err}")
    return False


def built_gifs_any(skin: str) -> dict[str, str] | None:
    """任意已构建好的高度缓存（就近显示用）。"""
    try:
        entries = sorted(os.listdir(CACHE_DIR), reverse=True)
    except OSError:
        return None
    for name in entries:
        if name.split("@")[0] == skin and "@" in name:
            got = built_gifs(skin, int(name.split("@")[1]))
            if got:
                return got
    return None


def start_build(skin: str, height: int, fps: int, log=None) -> "queue.Queue":
    """后台构建皮肤，返回结果队列；UI 线程轮询队列取 ("ok", paths) / ("err", msg)。
    不在 worker 线程里碰 tkinter（root.after 非线程安全）。"""
    q: "queue.Queue" = queue.Queue()

    def work():
        try:
            q.put(("ok", build_skin(skin, height, fps, log)))
        except Exception as e:
            q.put(("err", repr(e)))

    threading.Thread(target=work, daemon=True, name="deskpet-convert").start()
    return q


BuildKey = tuple[str, int, int]   # (skin, height, fps)


class SkinBuildManager:
    """skin build 去重（v4plan §11.4；v4.3 §6.5 waiter view set）：
    同一 (skin,height,fps) 只 pending 一次、只启动一个 converter；
    完成结果 fan-out 给所有等待中的 view；某个 view 中途换皮时
    通过 forget() 从 waiter set 撤销，不影响仍等待旧 key 的其他 view。

    全局最多一个 converter 子进程；不同 skin 不并发转换。
    v4.3 §9：导入（校验/copy2/manifest/catalog 刷新）与 build 共享
    同一条"最多 1 个 skin I/O job"的 lane（线程名 deskpet-convert）。
    """

    def __init__(self):
        # v4.3 §6.5：key → 等待中的 view id 集合（≤8 views）
        self._waiters: dict[BuildKey, set[str]] = {}
        self._queue: "queue.Queue | None" = None   # 当前唯一转换
        self._current: BuildKey | None = None
        self._pending: list[BuildKey] = []
        self._results: list[tuple[BuildKey, str, object]] = []
        self._last_paths: dict[BuildKey, dict] = {}   # 成功结果缓存
        # v4.3 §9 导入 lane（与 build 互斥；最多 1 运行 + 1 排队）
        self._import_thread: threading.Thread | None = None
        self._import_pending: tuple[str, str] | None = None
        self.on_import_result = None   # UI 回调：(ok, name, error)

    def request(self, view_id: str, skin: str, height: int,
                fps: int) -> BuildKey:
        """请求一个 build（幂等）；返回 build key。

        同一 key 被 N 个 view 请求只进入 pending 一次；结果 fan-out。
        """
        key = (str(skin), int(height), int(fps))
        if key in self._last_paths:
            self._results.append((key, "ok", self._last_paths[key]))
            return key
        waiters = self._waiters.get(key)
        if waiters is None:
            self._waiters[key] = {view_id}
            self._pending.append(key)
            self._maybe_start()
        else:
            waiters.add(view_id)
        return key

    def forget(self, view_id: str, key: BuildKey):
        """view 不再等待某 build（换皮/关闭时撤销等待）。"""
        waiters = self._waiters.get(key)
        if waiters is None:
            return
        waiters.discard(view_id)
        if not waiters:
            self._waiters.pop(key, None)
            if key in self._pending:
                self._pending.remove(key)

    def waiting_views(self, key: BuildKey) -> set[str]:
        return set(self._waiters.get(key, ()))

    def _maybe_start(self):
        if self._queue is not None or not self._pending:
            return
        if self._import_running():
            # 导入占用 lane：导入结束时由 _import_work 重启 build
            return
        key = self._pending.pop(0)
        self._current = key
        skin, height, fps = key
        self._queue = start_build(skin, height, fps, log=None)

    def poll_results(self) -> list[tuple[BuildKey, str, object]]:
        """非阻塞收割当前转换/导入结果；完成后启动下一个 pending。

        只允许 Tk 主线程调用（结果 fan-out 与 on_import_result 回调
        都在 UI 线程；worker 线程绝不触碰 Tk）。
        """
        if self._queue is not None:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self._queue = None
                key = self._current
                self._current = None
                self._waiters.pop(key, None)
                if kind == "ok":
                    self._last_paths[key] = payload
                self._results.append((key, kind, payload))
                self._start_next_job()
        results, self._results = self._results, []
        for key, kind, payload in results:
            if isinstance(key, tuple) and key and key[0] == "import":
                # 导入结果（ok, name, error）→ UI 回调
                if self.on_import_result is not None:
                    ok = kind == "import_ok"
                    try:
                        self.on_import_result(
                            ok, key[1],
                            "" if ok else str(payload))
                    except Exception:
                        pass
        return results

    def building(self) -> bool:
        return (self._queue is not None or bool(self._pending)
                or self._import_running())

    def results_pending(self) -> bool:
        """有未收割结果（如 _last_paths 记忆命中直接入队，building=False）。"""
        return bool(self._results)

    def pending_count(self) -> int:
        return (len(self._pending) + (1 if self._queue is not None else 0)
                + (1 if self._import_running() else 0))

    # ------------------------------------------------------------ 导入 lane（v4.3 §9）
    def _import_running(self) -> bool:
        thread = self._import_thread
        return thread is not None and thread.is_alive()

    def _start_next_job(self):
        """lane 空闲时的补位：排队的导入优先，其次 pending build。"""
        if self._queue is not None or self._import_running():
            return
        pending, self._import_pending = self._import_pending, None
        if pending is not None:
            self._start_import(*pending)
            return
        self._maybe_start()

    def submit_import(self, src_dir: str, name: str) -> None:
        """提交异步皮肤导入：校验/copy2/manifest 写入/catalog 刷新全部
        在单 background lane（与 build 互斥；忙时排队一个，覆盖旧待导入）。
        Tk 线程只做本方法（O(1) 排队），结果经 poll_results 收割。"""
        if self._import_running() or self._queue is not None:
            self._import_pending = (str(src_dir), str(name))
            return
        self._start_import(str(src_dir), str(name))

    def _start_import(self, src_dir: str, name: str):
        self._import_thread = threading.Thread(
            target=self._import_work, args=(src_dir, name),
            name="deskpet-convert", daemon=True)
        self._import_thread.start()

    def _import_work(self, src_dir: str, name: str):
        try:
            prepare_import(src_dir, name)
            refresh_skin_catalog()
            self._results.append((("import", name), "import_ok", name))
        except Exception as exc:
            self._results.append((("import", name), "import_err",
                                  str(exc)[:200]))
        self._import_thread = None
        self._start_next_job()


def prepare_import(src_dir: str, name: str) -> str:
    """复制素材 + 校验齐全 + 写标准 manifest。

    v4.3 §9：大文件 copy2 可能明显阻塞，只允许在 SkinBuildManager
    的导入 lane（deskpet-convert 线程）中调用，不再在 Tk 线程执行。
    """
    import shutil
    dst = os.path.join(PETS_DIR, name)
    os.makedirs(dst, exist_ok=True)
    for s in STATES:
        for ext in (".webm", ".mp4", ".gif", ".mkv", ".mov"):
            f = os.path.join(src_dir, s + ext)
            if os.path.isfile(f):
                shutil.copy2(f, os.path.join(dst, s + ext))
                break
    missing = [s for s in STATES
               if not any(os.path.isfile(os.path.join(dst, s + e))
                          for e in (".webm", ".mp4", ".gif", ".mkv", ".mov"))]
    if missing:
        raise RuntimeError("缺少素材: " + ", ".join(missing))
    with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "name": name, "title": name, "version": "1.0", "author": "imported",
            "animations": {s: {"loop": s in ("walk", "sleep"),
                               "repeat": 3 if s == "special" else None,
                               "note": {"walk": "工作中", "attack": "下达指令",
                                        "die": "等待批复", "special": "完成庆祝×3",
                                        "sleep": "无任务"}[s]}
                           for s in STATES},
        }, f, ensure_ascii=False, indent=2)
    return name
