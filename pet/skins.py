"""皮肤管理：发现、构建（转换）、缓存、导入——单 mutation lane + 事务。

v4.3.1 DP43-R04/R05/R06 重构合同（plan2 §8-§14）：

  * 唯一 skin mutation lane：build / rebuild / import / maintenance 共享
    "同一时刻 <= 1 个 active job"。lane 状态（active/pending/waiters/
    ready index）只允许 Tk / UiCoordinator 调用的 manager 方法修改；
    worker 线程只执行 job → 结果放入 bounded queue → 返回；
  * import 事务（§10）：名称/来源校验 → 同文件系统 staging → staging
    校验 → Windows 目录换名提交（live 先挪走，失败回滚）。import 失败
    保持旧 live skin 完整；同名 reimport 整目录替换，不混合 old/new
    文件、不残留旧扩展名素材；
  * cache 事务（§11）：ready 判定 = cache-manifest.json（schema/skin/
    height/fps/source_signature）+ 5 gif + 5 meta 完整证明。converter
    输出到 staging，完整成功 + manifest 最后写入后原子发布；失败保持
    旧 live cache，绝无 mixed old/new；
  * ready index：进程内唯一 cache 真值（替代 _last_paths）。只来自已
    验证 manifest；import 成功 / rebuild 发布 / maintenance reconcile
    时同步失效或重建；memo 命中不会再返回已删除 cache；
  * cancellation（§13）：converter 子进程树挂 Windows Job Object
    （JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE）+ 父进程 one-byte gate（加入
    job 前不 spawn ffmpeg）；DeskPet 退出或 obsolete build 可有界取消
    整棵转换树，不遗留孤儿 ffmpeg；
  * stop()：封口 lane（不再接受/启动新 job）、取消 active、有界等待。

注意：不要在本模块顶层 import tools.convert（会连带加载 PIL）。
转换全部走独立子进程 python -m tools.convert，主进程保持低内存。
"""
import enum
import json
import os
import queue
import shutil
import threading
import uuid
from dataclasses import dataclass

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

# ------------------------------------------------------------ 常量
MANIFEST_SCHEMA = 1
_IMPORT_PREFIX = ".deskpet-import-"
_OLD_PREFIX = ".deskpet-old-"
_BUILD_PREFIX = ".deskpet-build-"

# source 文件解析优先级（与 tools.convert._src_file 保持一致）
_SRC_EXT_PRIORITY = (".webm", ".mp4", ".mkv", ".mov", ".avi", ".gif")

# lane 结果队列与单次收割上限（active job <=1，正常远不满）
_JOB_QUEUE_MAX = 16

_CONVERT_TIMEOUT_SEC = 1800

# Windows 保留设备名（目录名不得使用；base 名含扩展名前缀也判保留）
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL",
     *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))})

# builtin 的固定 source 签名（生成器确定性：无磁盘 source 依赖）
_BUILTIN_SIGNATURE = {
    "builtin": {"name": BUILTIN_SKIN, "size": 0, "mtime_ns": 0}}


def validate_skin_name(name) -> str:
    """持久化 API boundary 的皮肤名校验（§10.1）；非法抛 ValueError。

    不依赖 UI file dialog 保证安全：非空、非 ./..、非绝对路径、无
    路径分隔符、非 BUILTIN_SKIN、非 Windows 保留设备名、无非法字符。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("皮肤名不能为空")
    if text in (".", ".."):
        raise ValueError("皮肤名不能是 . 或 ..")
    if os.path.isabs(text) or "/" in text or "\\" in text:
        raise ValueError("皮肤名不能包含路径分隔符")
    if text == BUILTIN_SKIN:
        raise ValueError(f"{BUILTIN_SKIN} 是保留的内置皮肤名")
    if text.rstrip(" .") != text or text.startswith("."):
        raise ValueError("皮肤名不能以点开头/结尾")
    if any(c in text for c in '<>:"|?*'):
        raise ValueError("皮肤名包含 Windows 非法字符")
    if text.split(".")[0].upper() in _WIN_RESERVED:
        raise ValueError("皮肤名是 Windows 保留设备名")
    return text


def _scan_skins() -> dict[str, dict]:
    """磁盘扫描（只允许在 SkinCatalog.refresh_from_disk 内/测试调用）。

    跳过点开头的 staging/old 目录（导入事务进行中不进入目录）。
    """
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
        if name.startswith("."):
            continue
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


# ================================================================ source 签名
def _src_file(src_dir: str, state: str) -> str | None:
    """按固定扩展名优先级解析一个 state 的 source 文件。"""
    for ext in _SRC_EXT_PRIORITY:
        p = os.path.join(src_dir, state + ext)
        if os.path.isfile(p):
            return p
    return None


def source_paths(skin_dir: str) -> dict[str, str] | None:
    """5 个 state 的当前 source 文件；任一缺失返回 None（后台线程用）。"""
    out = {}
    for s in STATES:
        p = _src_file(skin_dir, s)
        if p is None:
            return None
        out[s] = p
    return out


def source_signature(skin_dir: str) -> dict[str, dict] | None:
    """{state: {name,size,mtime_ns}}；source 不完整返回 None。"""
    paths = source_paths(skin_dir)
    if paths is None:
        return None
    sig = {}
    for state, p in paths.items():
        try:
            st = os.stat(p)
        except OSError:
            return None
        sig[state] = {"name": os.path.basename(p),
                      "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return sig


def source_signature_for(skin: str) -> dict[str, dict] | None:
    """皮肤当前 source 签名（builtin 用固定标记）。"""
    if skin == BUILTIN_SKIN:
        return dict(_BUILTIN_SIGNATURE)
    return source_signature(os.path.join(PETS_DIR, skin))


def signature_id(sig: dict | None) -> str:
    """source 签名的稳定短 id（generation；None 也确定）。"""
    import hashlib
    blob = json.dumps(sig, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ================================================================ cache manifest
def cache_manifest_path(cache_d: str) -> str:
    return os.path.join(cache_d, "cache-manifest.json")


def read_cache_manifest(cache_d: str) -> dict | None:
    """读取并结构校验 cache-manifest.json；缺失/损坏/字段不全 → None。"""
    try:
        with open(cache_manifest_path(cache_d), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("schema") != MANIFEST_SCHEMA:
        return None
    if not isinstance(m.get("skin"), str) or not isinstance(
            m.get("height"), int) or not isinstance(m.get("fps"), int):
        return None
    sig = m.get("source_signature")
    if not isinstance(sig, dict) or not sig:
        return None
    # 值结构校验（键集允许 builtin 固定标记或 5 state；与当前签名的
    # 等值比较由调用方完成）
    for entry in sig.values():
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("size"), int)
                or not isinstance(entry.get("mtime_ns"), int)):
            return None
    return m


def write_cache_manifest(cache_d: str, skin: str, height: int, fps: int,
                         sig: dict) -> None:
    """manifest 必须在全部产物完整后最后写入（事务提交标记）。"""
    payload = {
        "schema": MANIFEST_SCHEMA,
        "skin": str(skin),
        "height": int(height),
        "fps": int(fps),
        "source_signature": sig,
    }
    with open(cache_manifest_path(cache_d), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def cache_files_ok(cache_d: str) -> dict[str, str] | None:
    """5 gif + 5 meta 全部存在才返回路径表；否则 None。"""
    out = {}
    for s in STATES:
        p = os.path.join(cache_d, s + ".gif")
        if not os.path.isfile(p) or not os.path.isfile(p + ".json"):
            return None
        out[s] = p
    return out


def ready_cache_paths(skin: str, height: int,
                      fps: int) -> dict[str, str] | None:
    """完整 ready 判定（§11.2，后台线程用）：manifest + source 签名 +
    5 gif + 5 meta。不再只是 existence check。"""
    d = cache_dir(skin, height)
    m = read_cache_manifest(d)
    if m is None:
        return None
    if (m["skin"] != str(skin) or m["height"] != int(height)
            or m["fps"] != int(fps)):
        return None
    sig = source_signature_for(skin)
    if sig is None or m.get("source_signature") != sig:
        return None
    return cache_files_ok(d)


def legacy_ready_paths(skin: str, height: int,
                       fps: int) -> dict[str, str] | None:
    """旧 v4.3 无 manifest cache 的有条件接受（§11.5）。

    只有 per-state meta（converter 记录的 src_mtime/src_size/height/
    fps）完整证明"当前 source + fps + height"时才作为 legacy ready；
    否则返回 None（调用方安排 rebuild）。不为兼容无条件信任旧 cache。
    """
    d = cache_dir(skin, height)
    if os.path.isfile(cache_manifest_path(d)):
        return None   # 有 manifest 走正式 ready 路径
    paths = cache_files_ok(d)
    if paths is None:
        return None
    if skin == BUILTIN_SKIN:
        return paths   # builtin 生成器确定性，无需 source 匹配
    src = source_paths(os.path.join(PETS_DIR, skin))
    if src is None:
        return None
    for s in STATES:
        try:
            with open(paths[s] + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            st = os.stat(src[s])
        except (OSError, ValueError):
            return None
        if (meta.get("src_mtime") != st.st_mtime
                or meta.get("src_size") != st.st_size
                or meta.get("height") != int(height)
                or meta.get("fps") != int(fps)):
            return None
    return paths


# ================================================================ 目录事务
def _swap_directory(staging: str, live: str) -> None:
    """同 filesystem 原子目录替换（§10.4）。

    Python 官方：os.replace 到非空目录目标抛 OSError，因此用
    live → .deskpet-old-<uuid> → staging → live 的换名序列；第二步
    失败回滚 old → live。staging/live 必须同属一个父目录。
    """
    parent = os.path.dirname(live)
    old = os.path.join(parent, f"{_OLD_PREFIX}{uuid.uuid4().hex[:12]}")
    had_live = os.path.exists(live)
    if had_live:
        os.replace(live, old)
    try:
        os.replace(staging, live)
    except OSError:
        if had_live:
            os.replace(old, live)   # 回滚：旧 live 原样恢复
        raise
    shutil.rmtree(old, ignore_errors=True)   # 成功后清理旧目录


# ================================================================ converter 受控执行
def _create_kill_on_close_job():
    """Windows Job Object（KILL_ON_JOB_CLOSE）；不可用返回 None。

    官方依据：
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]

        class _EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMITS),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.windll.kernel32
        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return None
        info = _EXTENDED_LIMITS()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000：最后一个句柄
        # 关闭时终止 job 内整棵进程树（converter python + ffmpeg）。
        info.BasicLimitInformation.LimitFlags = 0x2000
        kernel32.SetInformationJobObject.argtypes = [
            wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        if not kernel32.SetInformationJobObject(
                hjob, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(hjob)
            return None
        kernel32.AssignProcessToJobObject.argtypes = [
            wt.HANDLE, wt.HANDLE]
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        return hjob
    except Exception:
        return None


class ConverterJob:
    """一次 converter 子进程树的受控执行（§13.2/§13.3）。

    gate 模式：converter 启动后先阻塞等 stdin 的 1 字节 gate；
    父进程 AssignProcessToJobObject 之后再放行——保证 converter 在
    加入 job 前不可能 spawn ffmpeg（无 assign race）。
    cancel()：Windows 关 job 句柄杀整棵树；其他平台退化为 kill
    直接子进程。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._hjob = None
        self.cancelled = False

    def cancel(self) -> None:
        """终止转换树（幂等；关闭 job 句柄 → KILL_ON_JOB_CLOSE）。"""
        with self._lock:
            if self.cancelled:
                return
            self.cancelled = True
            proc, hjob = self._proc, self._hjob
            self._proc = None
            self._hjob = None
        if hjob:
            import ctypes
            try:
                ctypes.windll.kernel32.CloseHandle(hjob)
            except Exception:
                pass
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self, src: str, out_dir: str, height: int, fps: int,
            log=None) -> bool:
        """执行 python -m tools.convert --gated；成功返回 True。"""
        import subprocess
        import sys
        if self.cancelled:
            return False
        exe = sys.executable
        # pythonw 没有 stdout，子进程里 print 会崩，优先用 python.exe
        if os.path.splitext(exe)[0].endswith("pythonw"):
            sibling = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(sibling):
                exe = sibling
        cmd = [exe, "-m", "tools.convert", "--gated",
               src, out_dir, str(height), str(fps)]
        cwd = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        hjob = _create_kill_on_close_job()
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            if hjob:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(hjob)
            if log:
                log(f"子进程转换异常: {e!r}")
            return False
        with self._lock:
            if self.cancelled:
                # cancel 先于注册到达：gate 从未放行，converter 还没
                # spawn 任何子进程，直接 kill 即可
                self._release_handles(proc, hjob)
                try:
                    proc.kill()
                except Exception:
                    pass
                return False
            self._proc, self._hjob = proc, hjob
        if hjob:
            import ctypes
            try:
                # gate 未放行前 converter 不会 spawn ffmpeg：无 assign race
                ctypes.windll.kernel32.AssignProcessToJobObject(
                    hjob, int(proc._handle))
            except Exception:
                pass
        try:
            proc.stdin.write(b"g")   # 放行 gate：开始真实工作
            proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            _out, err = proc.communicate(timeout=_CONVERT_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            self.cancel()
            try:
                proc.communicate(timeout=5.0)
            except Exception:
                pass
            if log:
                log("子进程转换超时（已终止）")
            return False
        finally:
            # 正常结束：关闭 job 句柄（KILL_ON_JOB_CLOSE 顺带清理 job
            # 内可能残留的进程；converter 退出码 0 时其 ffmpeg 必已结束）。
            # 取消路径：cancel() 已关句柄并清空槽位。
            with self._lock:
                hjob_done = self._hjob
                self._proc = None
                self._hjob = None
            if hjob_done is not None:
                import ctypes
                try:
                    ctypes.windll.kernel32.CloseHandle(hjob_done)
                except Exception:
                    pass
        # 实测（Windows）：KILL_ON_JOB_CLOSE 终止的进程 returncode==0，
        # 必须先看 cancelled 再看退出码，否则被取消的转换会误报成功。
        if self.cancelled:
            return False
        if proc.returncode == 0:
            return True
        if log:
            text = (err or b"").decode("utf-8", errors="replace")[-400:]
            log(f"子进程转换失败(code={proc.returncode}): {text}")
        return False


def build_skin(skin: str, height: int, fps: int, log=None, *,
               force: bool = False,
               converter: "ConverterJob | None" = None) -> dict[str, str]:
    """构建（或复用缓存）皮肤 GIF。耗时操作，只允许 skin lane worker 调用。

    v4.3.1 事务（§11.3）：converter / builtin 生成器输出到
    CACHE_DIR/.deskpet-build-<uuid> staging；全部 state 完整 → manifest
    最后写入 → 原子发布到 live；失败删除 staging、旧 live cache 保持
    完整——不会出现 mixed old/new live cache。force=True（rebuild）跳过
    ready/legacy 复用检查。
    """
    if not force:
        ready = ready_cache_paths(skin, height, fps)
        if ready:
            return ready
        legacy = legacy_ready_paths(skin, height, fps)
        if legacy is not None:
            # legacy ready：补写 manifest 升级为正式 ready（后台线程写盘）
            try:
                write_cache_manifest(cache_dir(skin, height), skin,
                                     height, fps, source_signature_for(skin))
            except OSError:
                pass
            return legacy
    staging = os.path.join(CACHE_DIR, f"{_BUILD_PREFIX}{uuid.uuid4().hex[:12]}")
    live = cache_dir(skin, height)
    try:
        if skin == BUILTIN_SKIN:
            _build_builtin_skin_into(staging, height)
        else:
            src = os.path.join(PETS_DIR, skin)
            if source_paths(src) is None:
                raise RuntimeError(f"皮肤 {skin} 素材不完整")
            if log:
                log(f"正在构建皮肤 {skin}（{height}px）…")
            job = converter if converter is not None else ConverterJob()
            if not job.run(src, staging, height, fps, log):
                raise RuntimeError(
                    f"皮肤 {skin} 构建失败：converter 子进程失败"
                    "（继续使用原皮肤）")
        built = cache_files_ok(staging)
        if built is None:
            raise RuntimeError(f"皮肤 {skin} 构建失败：staging 产物不完整")
        sig = source_signature_for(skin)
        if sig is None:
            raise RuntimeError(f"皮肤 {skin} 源签名不可用")
        write_cache_manifest(staging, skin, height, fps, sig)   # LAST
        os.makedirs(CACHE_DIR, exist_ok=True)
        _swap_directory(staging, live)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if log:
        log(f"皮肤 {skin} 构建完成")
    return cache_files_ok(live) or built


def _build_builtin_skin_into(out_dir: str, height: int) -> None:
    """builtin-cat：draw_cat + Pillow 生成单帧 GIF 与 meta（§10.4）。

    事务化：全部产物写入给定（staging）目录，由调用方原子发布。
    """
    from .icon import draw_cat

    height = max(96, min(960, int(height)))
    os.makedirs(out_dir, exist_ok=True)
    for state in STATES:
        out_gif = os.path.join(out_dir, state + ".gif")
        img = draw_cat(height).convert("P")
        img.save(out_gif, save_all=True, append_images=[],
                 duration=1000, loop=0)
        with open(out_gif + ".json", "w", encoding="utf-8") as f:
            json.dump({
                "frames": 1, "width": img.width, "height": img.height,
                "delay_ms": 1000, "loop": _BUILTIN_LOOP.get(state, True),
            }, f, ensure_ascii=False)


# 兼容入口：旧测试/诊断脚本直接调用（existence-only，无 manifest 校验）
def built_gifs(skin: str, height: int) -> dict[str, str] | None:
    """已构建的 GIF 路径表（existence check；完整 ready 判定用
    ready_cache_paths / SkinBuildManager.ready_paths）。"""
    return cache_files_ok(cache_dir(skin, height))


def built_gifs_any(skin: str) -> dict[str, str] | None:
    """任意已构建好的高度缓存（诊断用；Pet 加载路径请用
    SkinBuildManager.nearest_ready_cache——不在 Tk 做 listdir）。"""
    try:
        entries = sorted(os.listdir(CACHE_DIR), reverse=True)
    except OSError:
        return None
    for name in entries:
        if "@" in name and name.split("@")[0] == skin:
            got = cache_files_ok(os.path.join(CACHE_DIR, name))
            if got:
                return got
    return None


# ================================================================ 导入事务（§10）
def prepare_import(src_dir: str, name: str, cancel=None) -> str:
    """复制素材 + 校验齐全 + 写标准 manifest（完整事务，§10）。

    名称/来源校验 → 同 filesystem staging（PETS_DIR/.deskpet-import-*）
    → copy 5 states → manifest → staging 校验 → 目录换名提交。任何一步
    失败：旧 live skin 完整、staging 清理。只允许在 skin lane worker
    调用（大文件 copy2 阻塞）。
    """
    name = validate_skin_name(name)
    src_dir = str(src_dir)
    if not os.path.isdir(src_dir):
        raise RuntimeError(f"导入来源不存在：{src_dir}")
    # source 校验（touching live dst 之前）：5 states 必须在 src 内独立
    # 找全，绝不能用 dst 中旧文件补缺（§10.2）
    sources = {}
    for s in STATES:
        f = _src_file(src_dir, s)
        if f is None:
            raise RuntimeError(f"缺少素材: {s}")
        sources[s] = f
    os.makedirs(PETS_DIR, exist_ok=True)
    staging = os.path.join(PETS_DIR, f"{_IMPORT_PREFIX}{uuid.uuid4().hex[:12]}")
    try:
        os.makedirs(staging)
        for s in STATES:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("导入已取消")
            shutil.copy2(sources[s],
                         os.path.join(staging, os.path.basename(sources[s])))
        _write_import_manifest(staging, name)
        if source_paths(staging) is None:
            raise RuntimeError("导入校验失败：staging 素材不完整")
        _swap_directory(staging, os.path.join(PETS_DIR, name))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return name


def _write_import_manifest(dst: str, name: str) -> None:
    with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "name": name, "title": name, "version": "1.0",
            "author": "imported",
            "animations": {s: {"loop": s in ("walk", "sleep"),
                               "repeat": 3 if s == "special" else None,
                               "note": {"walk": "工作中", "attack": "下达指令",
                                        "die": "等待批复", "special": "完成庆祝×3",
                                        "sleep": "无任务"}[s]}
                           for s in STATES},
        }, f, ensure_ascii=False, indent=2)


# ================================================================ 维护任务（§14.4）
def run_maintenance(cancel=None) -> dict:
    """skin lane 的 maintenance job（后台线程）：

    - 清理 >1h 的 deskpet_conv_* 临时目录；
    - 清理 PETS_DIR / CACHE_DIR 内崩溃遗留的 .deskpet-* 目录；
    - 删除已不存在皮肤的 cache；
    - 每皮肤最多保留 2 个尺寸（旧尺寸删除）；
    - 校验全部 cache manifest，返回 ready reconciliation 结果：
      {"ready": {BuildKey: paths}, "seen": {(skin, height), ...}}。
    """
    import glob
    import tempfile
    import time

    def _stopped() -> bool:
        return cancel is not None and cancel.is_set()

    now = time.time()
    # 1) 系统 temp 里的 converter 临时目录
    try:
        tmp = tempfile.gettempdir()
        for d in glob.glob(os.path.join(tmp, "deskpet_conv_*")):
            if _stopped():
                break
            try:
                if now - os.path.getmtime(d) > 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass
    # 2) staging/old 遗留（正常路径都已清理；这些是崩溃残留）
    for root in (PETS_DIR, CACHE_DIR):
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            if _stopped():
                break
            if name.startswith(_IMPORT_PREFIX) or name.startswith(_OLD_PREFIX) \
                    or name.startswith(_BUILD_PREFIX):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    # 3) cache 目录整理
    known = set(list_skins())
    by_skin: dict[str, list[tuple[float, str, str]]] = {}
    ready: dict[tuple, dict[str, str]] = {}
    seen: set[tuple[str, int]] = set()
    try:
        entries = sorted(os.listdir(CACHE_DIR))
    except OSError:
        entries = []
    for name in entries:
        if _stopped():
            break
        path = os.path.join(CACHE_DIR, name)
        if not os.path.isdir(path) or "@" not in name:
            continue
        skin, _, height_s = name.partition("@")
        try:
            height = int(height_s)
        except ValueError:
            shutil.rmtree(path, ignore_errors=True)
            continue
        seen.add((skin, height))
        if skin not in known:
            # 皮肤已不存在：cache 一并删除
            shutil.rmtree(path, ignore_errors=True)
            continue
        try:
            by_skin.setdefault(skin, []).append(
                (os.path.getmtime(path), name, path))
        except OSError:
            pass
        # ready index reconciliation：manifest 完整才算 ready
        m = read_cache_manifest(path)
        if m is None:
            continue
        paths = cache_files_ok(path)
        if paths is None:
            continue
        sig = source_signature_for(skin)
        if sig is None or m.get("source_signature") != sig:
            continue
        ready[(skin, height, int(m["fps"]))] = paths
    # 4) 每皮肤最多保留 2 个尺寸（最新优先）
    for items in by_skin.values():
        if _stopped():
            break
        items.sort(reverse=True)
        for _mt, name, path in items[2:]:
            shutil.rmtree(path, ignore_errors=True)
            skin = name.partition("@")[0]
            seen.discard((skin, _height_of(name)))
    return {"ready": ready, "seen": seen}


def _height_of(cache_name: str) -> int:
    try:
        return int(cache_name.partition("@")[2])
    except ValueError:
        return -1


# ================================================================ Job lane（§9）
BuildKey = tuple[str, int, int]   # (skin, height, fps)


class SkinJobKind(enum.Enum):
    BUILD = "build"
    REBUILD = "rebuild"
    IMPORT = "import"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class SkinJobRequest:
    job_id: int
    kind: SkinJobKind
    key: BuildKey | None = None
    view_ids: tuple[str, ...] = ()
    src_dir: str = ""
    skin_name: str = ""


@dataclass(frozen=True)
class SkinJobResult:
    job_id: int
    kind: SkinJobKind
    ok: bool
    key: BuildKey | None = None
    payload: object = None
    error: str = ""


class _JobCancellation:
    """active job 的取消通道（Tk 发起、worker 消费）。

    event = 取消信号（import copy / maintenance 清理之间检查）；
    converter slot = BUILD job 注册的 ConverterJob.cancel 句柄。
    这不是 lane 状态：worker 写入 converter slot 受锁保护，且 register
    前已取消时立即执行取消回调（无 assign race）。
    """

    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._cancel_converter = None

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            fn = self._cancel_converter
            self._cancel_converter = None
        if fn is not None:
            try:
                fn()
            except Exception:
                pass

    def register_converter(self, converter: "ConverterJob") -> None:
        with self._lock:
            if self.event.is_set():
                already = True
            else:
                already = False
                self._cancel_converter = converter.cancel
        if already:
            converter.cancel()


class SkinBuildManager:
    """skin build 去重 + 单 mutation lane（v4plan §11.4；v4.3 §6.5/§9；
    v4.3.1 DP43-R04/R05 全面事务化）。

    同一 (skin,height,fps) 只 pending 一次、只启动一个 converter；
    完成结果 fan-out 给所有等待中的 view。lane 状态只由 Tk /
    UiCoordinator 调用的方法修改（§9.1 ownership）；worker 只执行
    job → 结果入 bounded queue → 返回。
    """

    def __init__(self):
        # v4.3 §6.5：key → 等待中的 view id 集合（≤8 views）
        self._waiters: dict[BuildKey, set[str]] = {}
        self._pending_builds: list[BuildKey] = []
        self._pending_rebuilds: list[BuildKey] = []
        # v4.3 §9 导入 lane（与 build 互斥；最多 1 运行 + 1 排队）
        self._pending_import: tuple[str, str] | None = None
        self._maintenance_pending = False
        # active job（worker 只读 request）
        self._active_job: SkinJobRequest | None = None
        self._active_thread: threading.Thread | None = None
        self._active_cancel: _JobCancellation | None = None
        self._job_results: "queue.Queue[SkinJobResult]" = queue.Queue(
            maxsize=_JOB_QUEUE_MAX)
        self._next_job_id = 1
        self._stopping = False
        # ready index：进程内唯一 cache 真值（替代 _last_paths，§11.6）。
        # _ready_stamps 记录每条目的发布序号（job id）——maintenance
        # reconcile 据此区分"已删除"与"扫描期间新发布"。
        self._ready_index: dict[BuildKey, dict[str, str]] = {}
        self._ready_stamps: dict[BuildKey, int] = {}
        self.on_import_result = None   # UI 回调：(ok, name, error)

    # ------------------------------------------------------------ Tk 安全查询
    def ready_paths(self, skin: str, height: int,
                    fps: int) -> dict[str, str] | None:
        """内存 ready index 查询（Tk 线程安全：无 I/O）。"""
        entry = self._ready_index.get((str(skin), int(height), int(fps)))
        return dict(entry) if entry is not None else None

    def nearest_ready_cache(self, skin: str, height: int,
                            fps: int) -> dict[str, str] | None:
        """就近高度回退（§12，内存 index：无 listdir/stat）。

        只允许 same skin + same fps + different height；index 条目
        全部来自当前 source generation 的已验证 manifest（import
        立即失效整个皮肤），旧 generation 不会作为新导入的回退。
        """
        want = (str(skin), int(fps))
        best_key = None
        best_delta = None
        for (s, h, f) in self._ready_index:
            if s == want[0] and f == want[1] and h != int(height):
                delta = abs(h - int(height))
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_key = (s, h, f)
        if best_key is None:
            return None
        return dict(self._ready_index[best_key])

    # ------------------------------------------------------------ 请求入口（Tk）
    def request(self, view_id: str, skin: str, height: int,
                fps: int) -> BuildKey:
        """请求一个 build（幂等）；返回 build key。

        同一 key 被 N 个 view 请求只进入 pending 一次；结果 fan-out。
        ready index 命中：立即入队一个 ok 结果（无 worker、无 I/O）。
        """
        key = (str(skin), int(height), int(fps))
        if self._stopping:
            return key
        cached = self._ready_index.get(key)
        if cached is not None:
            self._put_result(SkinJobResult(
                -1, SkinJobKind.BUILD, True, key, dict(cached), ""))
            return key
        waiters = self._waiters.get(key)
        if waiters is None:
            self._waiters[key] = {view_id}
            if key not in self._pending_builds:
                self._pending_builds.append(key)
            self._start_next_job()
        else:
            waiters.add(view_id)
            self._start_next_job()
        return key

    def request_rebuild(self, view_id: str, skin: str, height: int,
                        fps: int) -> BuildKey:
        """重建指定 key 的 cache（§11.4）：force build staging → 旧画面
        保持 → 完整后原子发布。不在 Tk rmtree live cache。"""
        key = (str(skin), int(height), int(fps))
        if self._stopping:
            return key
        # 立即失效 index 条目（正在显示的旧 PhotoImage 保留到新 build ok）
        self._ready_index.pop(key, None)
        self._waiters.setdefault(key, set()).add(view_id)
        self._pending_rebuilds.append(key)
        self._start_next_job()
        return key

    def forget(self, view_id: str, key: BuildKey):
        """view 不再等待某 build（换皮/关闭时撤销等待）。

        §13.4：最后一个 waiter 撤销且该 key 是 active build → cancel
        obsolete converter（避免旧转换阻塞唯一 lane 最长 1800s）。
        """
        waiters = self._waiters.get(key)
        if waiters is None:
            return
        waiters.discard(view_id)
        if not waiters:
            self._waiters.pop(key, None)
            if key in self._pending_builds:
                self._pending_builds.remove(key)
            if key in self._pending_rebuilds:
                self._pending_rebuilds.remove(key)
            job = self._active_job
            if (job is not None and job.kind is SkinJobKind.BUILD
                    and job.key == key):
                ctx = self._active_cancel
                if ctx is not None:
                    ctx.cancel()

    def waiting_views(self, key: BuildKey) -> set[str]:
        return set(self._waiters.get(key, ()))

    # ------------------------------------------------------------ 导入/维护（Tk）
    def submit_import(self, src_dir: str, name: str) -> None:
        """提交异步皮肤导入（Tk 线程只做 O(1) 排队；忙时排队一个，
        覆盖旧待导入）。导入在 lane worker 中以完整事务执行。"""
        if self._stopping:
            return
        if self._active_job is not None:
            self._pending_import = (str(src_dir), str(name))
            return
        self._enqueue_import(str(src_dir), str(name))

    def request_maintenance(self) -> None:
        """请求一次维护（§14.3 coalescing：100 次 request → <=1 pending）。"""
        if self._stopping:
            return
        self._maintenance_pending = True
        self._start_next_job()

    # ------------------------------------------------------------ 结果收割（Tk）
    def poll_results(self) -> list[tuple[BuildKey | tuple, str, object]]:
        """非阻塞收割 lane 结果；完成后启动下一个 job。

        只允许 Tk 主线程调用（结果 fan-out 与 on_import_result 回调
        都在 UI 线程；worker 线程绝不触碰 Tk，也绝不修改 lane 状态）。
        """
        drained: list[SkinJobResult] = []
        for _ in range(_JOB_QUEUE_MAX):
            try:
                drained.append(self._job_results.get_nowait())
            except queue.Empty:
                break
        active = self._active_job
        if active is not None:
            for res in drained:
                if res.job_id == active.job_id:
                    self._active_job = None
                    self._active_thread = None
                    self._active_cancel = None
                    break
        out: list[tuple] = []
        for res in drained:
            if res.kind in (SkinJobKind.BUILD, SkinJobKind.REBUILD):
                key = res.key
                self._waiters.pop(key, None)
                stamp = res.job_id if res.job_id > 0 else self._next_job_id
                if res.ok:
                    self._ready_index[key] = dict(res.payload)
                    self._ready_stamps[key] = stamp
                    out.append((key, "ok", dict(res.payload)))
                else:
                    self._ready_index.pop(key, None)
                    self._ready_stamps.pop(key, None)
                    out.append((key, "err", res.error or "skin job failed"))
            elif res.kind is SkinJobKind.IMPORT:
                # payload 恒为皮肤名（ok=已导入名；err=请求名）
                name = str(res.payload)
                if res.ok:
                    # §10.5：同名 import 成功 → 该皮肤全部 ready 代次失效
                    for k in [k for k in self._ready_index if k[0] == name]:
                        self._ready_index.pop(k, None)
                        self._ready_stamps.pop(k, None)
                    out.append((("import", name), "import_ok", name))
                else:
                    out.append((("import", name), "import_err", res.error))
            elif res.kind is SkinJobKind.MAINTENANCE:
                if res.ok and isinstance(res.payload, dict):
                    self._reconcile_ready_index(res.payload, res.job_id)
        if drained:
            self._start_next_job()
        for key, kind, payload in out:
            if isinstance(key, tuple) and key and key[0] == "import":
                # 导入结果（ok, name, error）→ UI 回调
                if self.on_import_result is not None:
                    ok = kind == "import_ok"
                    try:
                        self.on_import_result(
                            ok, key[1], "" if ok else str(payload))
                    except Exception:
                        pass
        return out

    def _reconcile_ready_index(self, payload: dict, job_id: int) -> None:
        """maintenance 结果合并（§11.6）。

        - verified（manifest+签名+文件全过）条目为准；
        - seen（磁盘仍存在）但未通过校验的 (skin,height) → 全部淘汰；
        - 未 seen 且发布序号早于本次扫描（stamp < job_id）→ 已被外部
          删除 → 淘汰（_last_paths 失效回归）；
        - 未 seen 且 stamp >= job_id → 扫描启动后新发布的条目 → 保留。
        """
        ready = payload.get("ready") or {}
        seen = payload.get("seen") or set()
        merged: dict[BuildKey, dict[str, str]] = {}
        stamps: dict[BuildKey, int] = {}
        for k, v in ready.items():
            merged[k] = dict(v)
            stamps[k] = job_id
        for key, paths in self._ready_index.items():
            if key in merged:
                continue
            if (key[0], key[1]) in seen:
                continue   # 目录仍在但校验未过 → 淘汰
            if self._ready_stamps.get(key, 0) >= job_id:
                merged[key] = paths   # 扫描期间/之后新发布 → 保留
                stamps[key] = self._ready_stamps.get(key, 0)
        self._ready_index = merged
        self._ready_stamps = stamps

    # ------------------------------------------------------------ 状态查询
    def building(self) -> bool:
        return (self._active_job is not None
                or bool(self._pending_builds)
                or bool(self._pending_rebuilds)
                or self._pending_import is not None
                or self._maintenance_pending
                or self.results_pending())

    def results_pending(self) -> bool:
        """有未收割结果（如 ready index 命中直接入队，building=False）。"""
        return not self._job_results.empty()

    def pending_count(self) -> int:
        return (len(self._pending_builds) + len(self._pending_rebuilds)
                + (1 if self._active_job is not None else 0)
                + (1 if self._pending_import is not None else 0)
                + (1 if self._maintenance_pending else 0))

    # ------------------------------------------------------------ shutdown（§13.1；DP43-R17）
    def request_stop(self) -> None:
        """只发停止信号（不 join）：封口 lane、清空 pending、取消
        active converter（Job Object 终止整棵转换树）。"""
        self._stopping = True
        self._pending_builds.clear()
        self._pending_rebuilds.clear()
        self._pending_import = None
        self._maintenance_pending = False
        ctx = self._active_cancel
        if ctx is not None:
            ctx.cancel()

    def join_for_shutdown(self, timeout: float) -> bool:
        """有界等待 active worker（timeout = 全局 deadline 剩余量）。

        timeout 后不假装 worker 已不存在（保留 ownership 引用）。
        """
        thread = self._active_thread
        exited = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
            exited = not thread.is_alive()
        if exited:
            self._active_thread = None
            self._active_job = None
            self._active_cancel = None
        return exited

    def stop(self, timeout: float = 0.75) -> None:
        """兼容薄 wrapper（测试/旧入口）：request_stop + bounded join。"""
        self.request_stop()
        self.join_for_shutdown(timeout)

    # ------------------------------------------------------------ 内部（Tk 线程）
    def _enqueue_import(self, src_dir: str, name: str) -> None:
        self._spawn(SkinJobRequest(
            self._next_job_id, SkinJobKind.IMPORT,
            src_dir=str(src_dir), skin_name=str(name)))

    def _put_result(self, res: SkinJobResult) -> None:
        """结果入 bounded queue（满时挤掉最旧，绝不阻塞调用线程）。"""
        try:
            self._job_results.put_nowait(res)
        except queue.Full:
            try:
                self._job_results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._job_results.put_nowait(res)
            except queue.Full:
                pass

    def _start_next_job(self) -> None:
        """lane 空闲时的补位（只允许 Tk 线程调用）。

        优先级：import（用户显式动作）> rebuild（用户显式动作）>
        build（view 等待画面）> maintenance（后台整理）。
        """
        if self._stopping or self._active_job is not None:
            return
        if self._pending_import is not None:
            src, name = self._pending_import
            self._pending_import = None
            self._enqueue_import(src, name)
            return
        if self._pending_rebuilds:
            key = self._pending_rebuilds.pop(0)
            if key in self._pending_builds:
                self._pending_builds.remove(key)
            self._spawn(SkinJobRequest(
                self._next_job_id, SkinJobKind.REBUILD, key=key))
            return
        if self._pending_builds:
            key = self._pending_builds.pop(0)
            self._spawn(SkinJobRequest(
                self._next_job_id, SkinJobKind.BUILD, key=key))
            return
        if self._maintenance_pending:
            self._maintenance_pending = False
            self._spawn(SkinJobRequest(
                self._next_job_id, SkinJobKind.MAINTENANCE))

    def _spawn(self, req: SkinJobRequest) -> None:
        self._next_job_id += 1
        self._active_job = req
        self._active_cancel = _JobCancellation()
        self._active_thread = threading.Thread(
            target=self._job_work, args=(req, self._active_cancel),
            name="deskpet-convert", daemon=True)
        self._active_thread.start()

    # ------------------------------------------------------------ worker（lane 线程）
    def _job_work(self, req: SkinJobRequest, ctx: _JobCancellation) -> None:
        """worker 唯一职责（§9.1）：执行 job → 发布 immutable result → 返回。

        绝不：start next / clear active / 调 Tk callback / 改 waiters。
        """
        try:
            ok, payload, error = self._execute_job(req, ctx)
        except Exception as exc:
            ok, payload, error = False, None, str(exc)[:200]
        self._put_result(SkinJobResult(
            req.job_id, req.kind, ok, req.key, payload, error))

    def _execute_job(self, req: SkinJobRequest,
                     ctx: _JobCancellation) -> tuple[bool, object, str]:
        if req.kind is SkinJobKind.IMPORT:
            try:
                name = prepare_import(req.src_dir, req.skin_name,
                                      cancel=ctx.event)
            except Exception as exc:
                # payload 携带皮肤名：UI 错误回调需要它
                return False, req.skin_name, str(exc)[:200]
            refresh_skin_catalog()
            return True, name, ""
        if req.kind is SkinJobKind.MAINTENANCE:
            return True, run_maintenance(cancel=ctx.event), ""
        # BUILD / REBUILD
        skin, height, fps = req.key
        converter = ConverterJob()
        ctx.register_converter(converter)
        paths = build_skin(skin, height, fps, force=(
            req.kind is SkinJobKind.REBUILD), converter=converter)
        if converter.cancelled:
            return False, None, "cancelled"
        return True, paths, ""
