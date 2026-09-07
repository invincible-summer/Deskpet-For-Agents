"""皮肤管理：发现、构建（转换）、缓存。

注意：不要在本模块顶层 import tools.convert（会连带加载 PIL）。
转换全部走独立子进程 python -m tools.convert，主进程保持低内存。
"""
import json
import os
import queue
import threading

STATES = ["walk", "attack", "die", "special", "sleep"]

from .config import CACHE_DIR, PETS_DIR


def list_skins() -> dict[str, dict]:
    """{皮肤名: manifest}。有 manifest.json 或五个素材齐全的目录都算。"""
    out: dict[str, dict] = {}
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
    优先用子进程跑转换（numpy/scipy 内存随子进程退出释放）。"""
    cached = built_gifs(skin, height)
    if cached:
        return cached
    src = os.path.join(PETS_DIR, skin)
    manifest = {}
    mf = os.path.join(src, "manifest.json")
    if os.path.isfile(mf):
        try:
            with open(mf, encoding="utf-8") as f:
                manifest = json.load(f)
        except ValueError:
            pass
    out_dir = cache_dir(skin, height)
    if log:
        log(f"正在构建皮肤 {skin}（{height}px）…")
    if not _convert_in_subprocess(src, out_dir, height, fps, log):
        from tools.convert import convert_skin  # 兜底：进程内转换（重导入）
        convert_skin(src, out_dir, height=height, fps=fps, manifest=manifest, log=log)
    built = built_gifs(skin, height)
    if not built:
        raise RuntimeError(f"皮肤 {skin} 构建失败")
    if log:
        log(f"皮肤 {skin} 构建完成")
    return built


def _convert_in_subprocess(src: str, out_dir: str, height: int, fps: int,
                           log=None) -> bool:
    """python -m tools.convert 子进程转换。成功返回 True。"""
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
            log(f"子进程转换异常，改用进程内转换: {e!r}")
        return False
    if r.returncode == 0:
        return True
    if log:
        err = r.stderr.decode("utf-8", errors="replace")[-400:]
        log(f"子进程转换失败(code={r.returncode})，改用进程内转换: {err}")
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


def prepare_import(src_dir: str, name: str) -> str:
    """皮肤导入第一步：复制素材 + 校验齐全 + 写标准 manifest（快速，可在 UI 线程调用）。"""
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
