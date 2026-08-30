"""Tkinter layout editor: drag the two screens to set their arrangement."""
import tkinter as tk
from tkinter import messagebox, ttk

from .config import Config, load, save
from .layout import Screen

SCALE = 0.12  # virtual pixels -> canvas pixels
CANVAS_W, CANVAS_H = 720, 420
COLORS = {"host": "#4a90d9", "client": "#d97b4a"}


class LayoutEditor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = tk.Tk()
        self.root.title("MouseShare — Screen Layout")
        self._build_ui()
        self._draw_screens()
        self._drag = None

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Host IP (for the client machine):").pack(side="left")
        self.ip_var = tk.StringVar(value=self.cfg.peer_host)
        ttk.Entry(top, textvariable=self.ip_var, width=16).pack(side="left", padx=4)
        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.port))
        ttk.Entry(top, textvariable=self.port_var, width=6).pack(side="left", padx=4)
        ttk.Button(top, text="Save", command=self._save).pack(side="right")

        self.canvas = tk.Canvas(self.root, width=CANVAS_W, height=CANVAS_H, bg="#222")
        self.canvas.pack(padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)

        hint = "Drag the screens so they touch along the edge where the mouse should cross."
        ttk.Label(self.root, text=hint, padding=(8, 0, 8, 8)).pack(fill="x")

    def _draw_screens(self) -> None:
        self.canvas.delete("all")
        # center the layout on the canvas
        xs = [s.x for s in self.cfg.screens.values()]
        ys = [s.y for s in self.cfg.screens.values()]
        xe = [s.x + s.w for s in self.cfg.screens.values()]
        ye = [s.y + s.h for s in self.cfg.screens.values()]
        off_x = CANVAS_W / 2 - (min(xs) + max(xe)) * SCALE / 2
        off_y = CANVAS_H / 2 - (min(ys) + max(ye)) * SCALE / 2
        self._offset = (off_x, off_y)
        self._items = {}
        for name, s in self.cfg.screens.items():
            x0, y0 = off_x + s.x * SCALE, off_y + s.y * SCALE
            x1, y1 = x0 + s.w * SCALE, y0 + s.h * SCALE
            rect = self.canvas.create_rectangle(
                x0, y0, x1, y1, fill=COLORS[name], outline="white", width=2, tags=name
            )
            label = f"{name}\n{s.w}x{s.h}"
            text = self.canvas.create_text(
                (x0 + x1) / 2, (y0 + y1) / 2, text=label, fill="white", tags=name
            )
            self._items[name] = (rect, text)

    def _screen_at(self, cx: float, cy: float):
        for name in self.cfg.screens:
            rect = self._items[name][0]
            x0, y0, x1, y1 = self.canvas.coords(rect)
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return name
        return None

    def _press(self, event) -> None:
        name = self._screen_at(event.x, event.y)
        self._drag = (name, event.x, event.y) if name else None

    def _motion(self, event) -> None:
        if self._drag is None:
            return
        name, px, py = self._drag
        dx, dy = event.x - px, event.y - py
        self._drag = (name, event.x, event.y)
        for item in self._items[name]:
            self.canvas.move(item, dx, dy)
        s = self.cfg.screens[name]
        self.cfg.screens[name] = Screen(
            int(s.x + dx / SCALE), int(s.y + dy / SCALE), s.w, s.h
        )

    def _save(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("MouseShare", "Port must be a number.")
            return
        self.cfg.peer_host = self.ip_var.get().strip()
        self.cfg.port = port
        self._snap_together()
        save(self.cfg)
        self._draw_screens()
        messagebox.showinfo("MouseShare", "Layout saved.")

    def _snap_together(self) -> None:
        """Snap the client to the nearest host edge so the screens touch."""
        host, client = self.cfg.screens["host"], self.cfg.screens["client"]
        gaps = {
            "right": abs(client.x - (host.x + host.w)),
            "left": abs((client.x + client.w) - host.x),
            "below": abs(client.y - (host.y + host.h)),
            "above": abs((client.y + client.h) - host.y),
        }
        side = min(gaps, key=gaps.get)
        x, y = client.x, client.y
        if side == "right":
            x = host.x + host.w
        elif side == "left":
            x = host.x - client.w
        elif side == "below":
            y = host.y + host.h
        else:
            y = host.y - client.h
        self.cfg.screens["client"] = Screen(x, y, client.w, client.h)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    LayoutEditor(load()).run()
