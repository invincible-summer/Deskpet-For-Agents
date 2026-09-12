from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "pet" / "dashboard.py"
text = p.read_text(encoding="utf-8")

old = '''        self._reflow_after = None
        self._last_reflow_width = -1
        self._last_reflow_dpi = self.metrics.dpi
        self.bind("<Configure>", self._on_configure)
'''
new = '''        self._reflow_after = None
        self._last_reflow_width = -1
        self._last_reflow_dpi = self.metrics.dpi
        # Deiconify/reopen can emit redundant same-size Configure events.
        # Reflow depends on size/DPI, not window position; suppressing these
        # avoids scheduling useless 50ms work on every retained-dashboard open.
        self._last_configure_size = None
        self.bind("<Configure>", self._on_configure)
'''
if old not in text:
    raise RuntimeError("dashboard reflow init not found")
text = text.replace(old, new, 1)

old = '''    def _on_configure(self, event):
        if event.widget is not self:
            return
        if self._reflow_after is not None:
            return
        self._reflow_after = self.after(50, self._reflow_debounced)
'''
new = '''    def _on_configure(self, event):
        if event.widget is not self:
            return
        size = (int(event.width), int(event.height))
        if size == self._last_configure_size:
            return
        self._last_configure_size = size
        if self._reflow_after is not None:
            return
        self._reflow_after = self.after(50, self._reflow_debounced)
'''
if old not in text:
    raise RuntimeError("dashboard configure handler not found")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8", newline="\n")
print("dashboard same-size configure suppression applied")
