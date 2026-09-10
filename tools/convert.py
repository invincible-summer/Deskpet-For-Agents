"""皮肤转换管线：webm/mp4/gif → 带透明索引的 GIF（tkinter 原生可解码）。

流程（转换期专用，运行时不依赖 numpy/scipy）：
  1. ffmpeg 抽帧（全分辨率 RGBA PNG 到临时目录）
  2. 背景检测：素材无 alpha 时，按角点取背景色；
     用“近背景色掩码 + 边缘连通域 + 形态学重建”精确抠出背景，
     不会误伤角色身上的深色部件（黑底素材的关键）
  3. 所有帧取内容并集 bbox，统一裁剪（保证动画对齐 + 去掉大黑边）
  4. 缩放到目标高度 → 255 色全局调色板量化（1 个透明哨兵索引）→ 打包 GIF
调色板中所有接近 MAGIC 透明色的条目都会被轻微扰动，
保证桌宠身体颜色不会与透明色冲突产生“洞”。
"""
import json
import os
import shutil
import tempfile

from PIL import Image

# 注意：numpy/scipy 在函数内惰性导入——运行中的桌宠进程只引用本模块的常量，
# 重转换在独立子进程（python -m tools.convert）中进行，转换内存随子进程退出释放。

STATES = ["walk", "attack", "die", "special", "sleep"]
MAGIC = (0x10, 0x10, 0x11)        # tkinter transparentcolor（避开纯黑）
SENTINEL = (255, 0, 255)          # 调色板里的透明哨兵色
MAX_FRAMES = 240
PAD = 10
VIDEO_EXT = (".webm", ".mp4", ".mkv", ".mov", ".avi")


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _src_file(src_dir: str, state: str) -> str | None:
    for ext in VIDEO_EXT + (".gif",):
        p = os.path.join(src_dir, state + ext)
        if os.path.isfile(p):
            return p
    return None


def _extract_frames(src: str, tmp: str, fps: int) -> int:
    ff = find_ffmpeg()
    cmd = [ff, "-y", "-i", src, "-vf", f"fps={fps}", "-start_number", "1",
           "-frames:v", str(MAX_FRAMES), os.path.join(tmp, "%04d.png")]
    import subprocess
    r = subprocess.run(cmd, capture_output=True, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    frames = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
    if not frames:
        err = (r.stderr or b"")[-500:].decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg 抽帧失败: {err}")
    return len(frames)


def _corner_bg(arr):
    """arr: HxWx4 ndarray。四角颜色一致则返回背景色，否则 None。"""
    import numpy as np
    h, w = arr.shape[:2]
    k = max(4, min(w, h) // 24)
    pts = []
    for x, y in ((0, 0), (w - k, 0), (0, h - k), (w - k, h - k)):
        px = arr[y:y + k, x:x + k].reshape(-1, 4).astype(np.float32)
        pts.append(px.mean(axis=0))
    d = max(float(np.sqrt(((a - b) ** 2).sum())) for a in pts for b in pts)
    if d < 40:
        mean = np.mean(pts, axis=0)
        return tuple(int(c) for c in mean[:3])
    return None


def _bg_mask(arr, bg) -> "np.ndarray":
    """bool 掩码：True=背景像素。边缘连通域 + 重建，保护角色内部深色。"""
    import numpy as np
    from scipy import ndimage
    rgb = arr[:, :, :3].astype(np.int32)
    dist = np.sqrt(((rgb - np.array(bg, dtype=np.int32)) ** 2).sum(axis=2))
    lum = float(bg[0]) * 0.299 + float(bg[1]) * 0.587 + float(bg[2]) * 0.114
    t_strict, t_loose = (14, 34) if lum < 60 else (70, 130)

    strict = dist <= t_strict
    # 腐蚀 2px，切断角色与边框之间的细黑桥（如贴边的深色脚部）；
    # border_value=1 保证图像边缘处不被腐蚀，边框环形采样才能命中背景
    eroded = ndimage.binary_erosion(strict, iterations=2, border_value=1)
    labels, n = ndimage.label(eroded)
    if n == 0:
        return np.zeros(arr.shape[:2], dtype=bool)
    border = np.zeros((eroded.shape[0], eroded.shape[1]), dtype=bool)
    border[0, :] = border[-1, :] = True
    border[:, 0] = border[:, -1] = True
    border_ids = np.unique(labels[border & eroded])
    border_ids = border_ids[border_ids != 0]
    seed = np.isin(labels, border_ids)
    # 重建：把被腐蚀掉的真实背景长回来（宽松阈值允许吃掉边缘暗晕）
    loose = dist <= t_loose
    bgm = ndimage.binary_propagation(seed, mask=loose)
    return bgm


def _has_alpha(im: Image.Image) -> bool:
    lo, hi = im.getchannel("A").getextrema()
    return lo < 250


def _union_bbox(masks, w: int, h: int) -> tuple[int, int, int, int]:
    import numpy as np
    union = np.zeros((h, w), dtype=bool)
    for m in masks:
        union |= ~m
    ys, xs = np.where(union)
    if len(xs) == 0:
        return (0, 0, w, h)
    x0, x1 = max(0, int(xs.min()) - PAD), min(w, int(xs.max()) + 1 + PAD)
    y0, y1 = max(0, int(ys.min()) - PAD), min(h, int(ys.max()) + 1 + PAD)
    return (x0, y0, x1, y1)


def _build_palette(sample_rgbs: list[Image.Image]) -> Image.Image:
    tile = 64
    strip = Image.new("RGB", (tile, tile * len(sample_rgbs) + 1))
    for i, rgb in enumerate(sample_rgbs):
        strip.paste(rgb.resize((tile, tile)), (0, i * tile))
    strip.paste(Image.new("RGB", (tile, 1), SENTINEL), (0, tile * len(sample_rgbs)))
    pal_img = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT)

    pal = pal_img.getpalette() or []
    # 统一为 256 项，并把哨兵色强制钉在索引 255（与 _quantize_frame 的确定性透明配套）
    pal = (pal + [0] * 768)[:768]
    pal[765:768] = list(SENTINEL)
    # 扰动接近 MAGIC 的调色板条目，防止身体色与透明色撞色
    for i in range(0, 765, 3):
        c = pal[i:i + 3]
        if sum(abs(c[j] - MAGIC[j]) for j in range(3)) < 48:
            pal[i + 2] = min(255, pal[i + 2] + 40)
    pal_img.putpalette(pal)
    return pal_img


def _quantize_frame(rgba: Image.Image, pal_img: Image.Image) -> Image.Image:
    rgb = Image.new("RGB", rgba.size, SENTINEL)
    rgb.paste(rgba, mask=rgba.getchannel("A"))
    q = rgb.quantize(palette=pal_img, dither=Image.Dither.NONE)
    # 确定性透明：不管量化器怎么映射，透明像素一律写入哨兵索引 255
    tmask = rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    q.paste(255, mask=tmask)
    return q


def convert_state(src: str, out_gif: str, height: int, fps: int, loop: bool) -> dict:
    """转换单个状态，返回 meta。逐帧处理，只保留量化后的小帧，峰值内存可控。"""
    import numpy as np
    os.makedirs(os.path.dirname(out_gif), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="deskpet_conv_")
    try:
        if src.lower().endswith(".gif"):
            im = Image.open(src)
            n, i = 0, 0
            while True:
                try:
                    im.seek(i)
                except EOFError:
                    break
                im.convert("RGBA").save(os.path.join(tmp, "%04d.png" % (i + 1)))
                i += 1
                n = i
                if i >= MAX_FRAMES:
                    break
        else:
            n = _extract_frames(src, tmp, fps)

        names = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))

        def load_keyed(name: str) -> Image.Image:
            im = Image.open(os.path.join(tmp, name)).convert("RGBA")
            if bg is None:
                return im
            arr = np.asarray(im)
            m = _bg_mask(arr, bg)
            arr = arr.copy()
            arr[m, 3] = 0
            return Image.fromarray(arr, "RGBA")

        # 样本帧：背景色 + 掩码 + 并集 bbox
        sample_idx = list(range(0, len(names), max(1, len(names) // 8)))[:8]
        samples = [np.asarray(Image.open(os.path.join(tmp, names[i])).convert("RGBA"))
                   for i in sample_idx]
        bg = _corner_bg(samples[len(samples) // 2])
        sample_masks = []
        for arr in samples:
            if bg is None:
                sample_masks.append(np.zeros(arr.shape[:2], dtype=bool))
            else:
                sample_masks.append(_bg_mask(arr, bg))
        bbox = _union_bbox(sample_masks, samples[0].shape[1], samples[0].shape[0])

        def fit(rgba: Image.Image) -> Image.Image:
            rgba = rgba.crop(bbox)
            if rgba.height > height:
                w = max(2, round(rgba.width * height / rgba.height))
                rgba = rgba.resize((w, height), Image.LANCZOS)
            return rgba

        # 调色板样本（前若干帧）
        pal_n = min(len(names), 10)
        pal_samples = [fit(load_keyed(names[i])).convert("RGB") for i in range(pal_n)]
        pal_img = _build_palette(pal_samples)
        del pal_samples

        q_frames = [_quantize_frame(fit(load_keyed(name)), pal_img) for name in names]
        delay = max(33, round(1000 / max(2, fps)))
        q_frames[0].save(
            out_gif, save_all=True, append_images=q_frames[1:],
            duration=delay, loop=0 if loop else 1,
            transparency=255, disposal=2, optimize=False,
        )
        meta = {
            "frames": len(q_frames), "width": q_frames[0].width, "height": q_frames[0].height,
            "delay_ms": delay, "loop": loop,
            "src": os.path.basename(src),
            "src_mtime": os.stat(src).st_mtime, "src_size": os.stat(src).st_size,
            "height": height, "fps": fps,
        }
        with open(out_gif + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _find_sentinel(pal_img: Image.Image) -> int:
    pal = pal_img.getpalette() or []
    for i in range(0, len(pal), 3):
        if tuple(pal[i:i + 3]) == SENTINEL:
            return i // 3
    return 255


def convert_skin(src_dir: str, out_dir: str, height: int = 240, fps: int = 12,
                 manifest: dict | None = None, log=None) -> dict:
    """转换整个皮肤（5 个状态）。已转换且源未变的自动跳过。"""
    def say(msg):
        if log:
            log(msg)

    os.makedirs(out_dir, exist_ok=True)
    done = {}
    for state in STATES:
        src = _src_file(src_dir, state)
        if not src:
            say(f"缺少 {state} 素材，跳过")
            continue
        out_gif = os.path.join(out_dir, state + ".gif")
        meta_path = out_gif + ".json"
        st = os.stat(src)
        if os.path.isfile(meta_path) and os.path.isfile(out_gif):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    old = json.load(f)
                if (old.get("src_mtime") == st.st_mtime and old.get("src_size") == st.st_size
                        and old.get("height") == height and old.get("fps") == fps):
                    done[state] = old
                    continue
            except (OSError, ValueError):
                pass
        loop = bool((manifest or {}).get("animations", {}).get(state, {}).get("loop", True))
        say(f"转换 {state}: {os.path.basename(src)} …")
        done[state] = convert_state(src, out_gif, height, fps, loop)
        say(f"{state} 完成：{done[state]['frames']} 帧 {done[state]['width']}x{done[state]['height']}")
    return done


def _selftest() -> None:
    """CLI 入口：python -m tools.convert [--gated] <skin_dir> <out_dir> [height] [fps]
    在独立子进程中执行转换，numpy/scipy 内存随进程退出释放。

    --gated（v4.3.1 DP43-R05）：启动后先阻塞读 stdin 的 1 字节 gate
    再做任何真实工作。父进程先 AssignProcessToJobObject（Windows Job
    Object，KILL_ON_JOB_CLOSE）再放行 gate——保证 converter 在加入 job
    前绝不可能 spawn ffmpeg（无 assign race，DeskPet 退出可杀整棵树）。
    """
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--gated" in sys.argv[1:]:
        try:
            sys.stdin.buffer.read(1)   # 等 gate 字节
        except Exception:
            pass
    if len(args) < 2:
        print("usage: python -m tools.convert [--gated] <skin_dir> <out_dir> [height] [fps]")
        raise SystemExit(1)
    src_dir, out_dir = args[0], args[1]
    height = int(args[2]) if len(args) > 2 else 240
    fps = int(args[3]) if len(args) > 3 else 12
    convert_skin(src_dir, out_dir, height=height, fps=fps,
                 log=print if sys.stdout else None)


if __name__ == "__main__":
    _selftest()
