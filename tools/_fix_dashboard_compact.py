from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "pet" / "dashboard.py"
text = p.read_text(encoding="utf-8")

text = text.replace(
    '        config_path = str(getattr(app.config, "path", runtime_paths.config_file))\n',
    '',
    1,
)

old = '''        tk.Label(savep.body,
                 text=f"配置文件位置（只读展示）：{config_path}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9)).pack(anchor="w", pady=(6, 0))
        drow = tk.Frame(savep.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(5, 0))
        tk.Label(drow, text=f"本地数据：{runtime_paths.data_root}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9), anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="right")
'''
new = '''        drow = tk.Frame(savep.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(6, 0))
        tk.Label(drow, text=f"配置与素材目录：{runtime_paths.data_root}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9), anchor="w").pack(
            side="left", fill="x", expand=True)
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="right")
'''
if old not in text:
    raise RuntimeError("expanded local-data rows not found")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8", newline="\n")
print("compact dashboard data row applied")
