# Third-party components

DeskPet portable builds bundle third-party runtime components. The exact package
versions are pinned by `constraints.txt` / `requirements-build.txt` and must be
verified from the produced distribution before each release.

Runtime families currently include CPython runtime components, Pillow, psutil,
comtypes, NumPy, SciPy, imageio-ffmpeg and the FFmpeg executable supplied through
imageio-ffmpeg. Nuitka is the build tool.

Before publishing a binary release, the release job must retain the final build
report and the actual FFmpeg licensing/build configuration. In particular, an
FFmpeg build containing `--enable-nonfree` must not be published.

This notice is informational and does not replace the license files/notices
shipped by the respective upstream components where redistribution requires them.
