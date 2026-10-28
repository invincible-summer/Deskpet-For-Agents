# DeskPet application icon

Original artwork created for this project with the built-in imagegen tool on
2026-10-27. This icon is independent of user-supplied pet skins and references no
existing character or external brand.

- `deskpet-source.png`: untouched generated transparent master.
- `deskpet.png`: 256px RGBA runtime image for the dashboard and window icons.
- `deskpet.ico`: the same artwork in 16, 20, 24, 32, 40, 48, 64, 128 and 256px
  frames for the tray and executable. Pillow only resized and encoded the master.

Only the runtime PNG and ICO are bundled into compiled builds. The tray copies
the immutable ICO to its existing LocalAppData cache, replacing older artwork
atomically when the content changes.

Generation prompt:

> Use case: logo-brand. Asset type: original Windows desktop companion application
> icon, used in a 32–40px dashboard brand mark and a 16–32px system tray. Create one
> polished original cute cat companion emblem for DeskPet: a compact round
> warm-ivory cat face with two small rounded triangular ears, deep teal outer
> silhouette, calm expressive dark eyes and tiny friendly mouth. A subtle
> integrated terminal cue may be formed by a simple teal forehead marking shaped
> like a small chevron, without actual text. Style: premium clean app icon
> illustration, gently rounded crafted shapes, restrained soft highlights, strong
> silhouette, balanced negative space, high contrast and legible when reduced to
> 16px. Use the app's existing deep teal and warm off-white visual language, with
> just a very small warm coral inner-ear accent. Center a single front-facing cat
> head, filling about 88 percent of the square canvas. Transparent background
> outside the emblem, genuinely transparent alpha, no surrounding tile, no shadows
> outside the silhouette, no text, no letters, no numbers, no extra objects, no
> watermark, no existing character or external brand references. Crisp professional
> finished asset.

Native icon size/loading follows Microsoft's
[LoadImageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-loadimagew)
and [GetSystemMetrics](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getsystemmetrics)
contracts. HICON ownership remains with the tray worker.
