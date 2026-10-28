import csv
import hashlib
import os
import queue
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import mss
    from PIL import Image, ImageTk
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "Не установлены зависимости. Запустите install.bat и повторите запуск.\n"
        f"Подробности: {exc}"
    )

APP_TITLE = "Majestic Roulette Analyzer"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT_DIR, exist_ok=True)

# Цвета столбцов в истории слева направо: 2X, 3X, 5X, 10X.
MULTIPLIERS = ["2X", "3X", "5X", "10X"]
DEFAULT_INTERVAL = 2.0


def rgb_to_hsv_pixels(rgb):
    """Convert RGB image array to HSV-like channels using Pillow."""
    hsv = np.asarray(Image.fromarray(rgb.astype("uint8"), "RGB").convert("HSV"))
    return hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]


class RegionSelector:
    """Full-screen drag selector. Returns (left, top, width, height) or None."""

    def __init__(self, parent, callback):
        self.parent = parent
        self.callback = callback
        self.win = tk.Toplevel(parent)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.28)
        self.win.configure(bg="black")
        self.win.overrideredirect(True)
        self.canvas = tk.Canvas(self.win, cursor="crosshair", bg="black",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            25, 25, anchor="nw",
            text="Выделите всю полоску истории цветных результатов. Esc — отмена.",
            fill="white", font=("Segoe UI", 16, "bold")
        )
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)
        self.win.bind("<Escape>", lambda _e: self.cancel())
        self.win.focus_force()

    def on_down(self, event):
        self.start = (event.x_root, event.y_root)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00ff99", width=3
        )

    def on_move(self, event):
        if self.start and self.rect:
            x0 = self.start[0] - self.win.winfo_rootx()
            y0 = self.start[1] - self.win.winfo_rooty()
            self.canvas.coords(self.rect, x0, y0, event.x, event.y)

    def on_up(self, event):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = event.x_root, event.y_root
        left, top = min(x0, x1), min(y0, y1)
        width, height = abs(x1 - x0), abs(y1 - y0)
        self.win.destroy()
        if width < 8 or height < 8:
            self.callback(None)
        else:
            self.callback((left, top, width, height))

    def cancel(self):
        self.win.destroy()
        self.callback(None)


class RouletteAnalyzer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x700")
        self.minsize(760, 600)
        self.configure(bg="#111315")
        self.region = None
        self.running = False
        self.worker = None
        self.events = queue.Queue()
        self.seen_signatures = set()
        self.history = []
        self.csv_path = os.path.join(OUT_DIR, "roulette_history.csv")
        self.last_capture = None
        self.calibration_active = False
        self.calibration_target = 50
        self.calibration_count = 0
        self.calibration_session_mode = False
        self.calibration_session_signatures = set()
        self.round_seconds = 15.0
        self.next_round_capture = None
        self.latest_detection = None
        self.stable_candidate = None
        self.stable_candidate_count = 0
        self.stable_multiplier = None
        self.last_history_sequence = None
        self.pending_history_sequence = None
        self.pending_history_count = 0
        self._build_ui()
        self.after(200, self._drain_queue)
        self._load_existing_csv()

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#111315")
        style.configure("TLabel", background="#111315", foreground="#e8e8e8",
                        font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"),
                        foreground="#ffffff")
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        style.configure("TCheckbutton", background="#111315", foreground="#e8e8e8")
        style.configure("Treeview", background="#202326", foreground="#f0f0f0",
                        fieldbackground="#202326", rowheight=27)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Majestic Roulette Analyzer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="Режим истории: выделите всю верхнюю полоску цветных результатов слева направо, включая все видимые ячейки, но не таймер и не фон.",
        ).pack(anchor="w", pady=(4, 14))

        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Button(controls, text="1. Выбрать всю полоску истории", command=self.select_region).pack(side="left")
        self.region_label = ttk.Label(controls, text="Область не выбрана")
        self.region_label.pack(side="left", padx=12)

        controls2 = ttk.Frame(root)
        controls2.pack(fill="x", pady=(0, 12))
        ttk.Label(controls2, text="Сканирование (сек.):").pack(side="left")
        self.interval_var = tk.StringVar(value="0.35")
        ttk.Entry(controls2, textvariable=self.interval_var, width=6).pack(side="left", padx=5)
        ttk.Label(controls2, text="Раунд (сек., справочно):").pack(side="left")
        self.round_var = tk.StringVar(value="15")
        ttk.Entry(controls2, textvariable=self.round_var, width=5).pack(side="left", padx=5)
        self.start_btn = ttk.Button(controls2, text="Начать мониторинг", command=self.start_monitoring)
        self.start_btn.pack(side="left", padx=(6, 3))
        self.stop_btn = ttk.Button(controls2, text="Остановить", command=self.stop_monitoring, state="disabled")
        self.stop_btn.pack(side="left", padx=3)
        ttk.Button(controls2, text="Калибровка: 50 раундов", command=self.start_calibration).pack(side="left", padx=3)
        ttk.Button(controls2, text="Проверить цвет", command=self.scan_once).pack(side="left", padx=3)
        ttk.Button(controls2, text="Экспорт CSV…", command=self.export_csv).pack(side="right")

        self.status_var = tk.StringVar(value="Готово. Выберите всю верхнюю полоску истории результатов.")
        ttk.Label(root, textvariable=self.status_var, foreground="#8fe3b1").pack(anchor="w", pady=(0, 4))
        self.calibration_var = tk.StringVar(value="Калибровка: не запущена (цель — 50 раундов).")
        ttk.Label(root, textvariable=self.calibration_var, foreground="#f3d37a").pack(anchor="w", pady=(0, 8))

        self.preview_label = ttk.Label(root, text="Предпросмотр выбранной области появится здесь.")
        self.preview_label.pack(fill="x", pady=(0, 8))

        stats_frame = ttk.LabelFrame(root, text="Историческая статистика (только подтверждённые результаты)", padding=8)
        stats_frame.pack(fill="x", pady=(0, 8))
        self.stats_var = tk.StringVar(value="Подтверждено результатов: 0. Сначала проверьте распознавание.")
        ttk.Label(stats_frame, textvariable=self.stats_var, wraplength=850).pack(anchor="w")
        self.recommendation_var = tk.StringVar(value="Сигнал: пока недостаточно проверенных данных.")
        ttk.Label(stats_frame, textvariable=self.recommendation_var, wraplength=850,
                  foreground="#f3d37a").pack(anchor="w", pady=(4, 0))
        self.prob_vars = {}
        prob_row = ttk.Frame(stats_frame)
        prob_row.pack(fill="x", pady=(5, 0))
        for mult in MULTIPLIERS:
            v = tk.StringVar(value=f"{mult}: —")
            self.prob_vars[mult] = v
            ttk.Label(prob_row, textvariable=v, width=18).pack(side="left", padx=3)

        editbar = ttk.Frame(root)
        editbar.pack(fill="x", pady=(0, 6))
        ttk.Button(editbar, text="Подтвердить выбранный", command=self.confirm_selected).pack(side="left")
        ttk.Button(editbar, text="Исправить результат…", command=self.edit_selected).pack(side="left", padx=6)
        ttk.Label(editbar, text="Выбери строку и подтверди/исправь её вручную.", foreground="#b8bec4").pack(side="left", padx=8)

        cols = ("time", "multiplier", "confidence", "verified", "signature")
        self.table = ttk.Treeview(root, columns=cols, show="headings", height=10)
        self.table.heading("time", text="Время обнаружения")
        self.table.heading("multiplier", text="Распознанный результат")
        self.table.heading("confidence", text="Оценка распознавания")
        self.table.heading("verified", text="Проверен")
        self.table.heading("signature", text="ID")
        self.table.column("time", width=150, anchor="w")
        self.table.column("multiplier", width=150, anchor="center")
        self.table.column("confidence", width=130, anchor="center")
        self.table.column("verified", width=85, anchor="center")
        self.table.column("signature", width=200, anchor="w")
        self.table.pack(fill="both", expand=True)

        foot = (
            "ВАЖНО: программа сравнивает последовательность видимых ячеек истории. Одинаковые цвета подряд учитываются, если обновление полоски можно сопоставить со сдвигом. "
            "Если вся видимая последовательность не меняется (например, все ячейки одинаковые), по одним цветам невозможно подтвердить новый раунд. "
            "Автоматические записи проверяйте вручную; исторические частоты не гарантируют следующий исход."
        )
        ttk.Label(root, text=foot, wraplength=850, foreground="#b8bec4").pack(anchor="w", pady=(8, 0))

    def select_region(self):
        self.stop_monitoring()
        self.status_var.set("Выделите всю верхнюю полоску истории мышью…")
        self.withdraw()
        self.after(250, lambda: RegionSelector(self, self._region_selected))

    def _region_selected(self, region):
        self.deiconify()
        self.lift()
        if not region:
            self.status_var.set("Выбор отменён или область слишком мала. Выделите всю полоску цветных ячеек.")
            return
        self.region = region
        self.region_label.config(text=f"x={region[0]}, y={region[1]}, {region[2]}×{region[3]}")
        self.status_var.set("Полоска выбрана. Проверьте, что в предпросмотре видны все цветные ячейки.")
        self.scan_once()

    def _capture(self):
        if not self.region:
            raise ValueError("Сначала выделите всю полоску истории результатов.")
        left, top, width, height = self.region
        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
            arr = np.asarray(shot)[:, :, :3][:, :, ::-1].copy()  # BGRA -> RGB
        return arr

    def _analyze_image(self, rgb):
        """Detect the visible colored tiles in a horizontal history strip."""
        h, w, _ = rgb.shape
        if h < 8 or w < 20:
            return [], "Область слишком мала. Выделите всю верхнюю полоску истории."

        # Use the central vertical band to avoid rounded corners and surrounding UI.
        band = rgb[max(1, int(h * 0.20)):max(2, int(h * 0.80)), :, :]
        hsv = np.asarray(Image.fromarray(band.astype("uint8"), "RGB").convert("HSV"))
        sat = hsv[:, :, 1].astype(float)
        val = hsv[:, :, 2].astype(float)
        # Colored tiles have saturation; grey 2X tiles are bright but nearly neutral.
        active = ((val > 75) & ((sat > 45) | ((sat < 55) & (val > 85)))).mean(axis=0) > 0.45

        # Close tiny gaps in the per-column mask, then extract contiguous tile segments.
        for i in range(1, len(active) - 1):
            if not active[i] and active[i - 1] and active[i + 1]:
                active[i] = True
        edges = np.diff(np.r_[False, active, False].astype(np.int8))
        starts = np.where(edges == 1)[0]
        ends = np.where(edges == -1)[0]
        segments = [(int(a), int(b)) for a, b in zip(starts, ends) if b - a >= max(4, int(w * 0.012))]

        results = []
        for x0, x1 in segments:
            # Sample the interior of each detected tile.
            tile = rgb[max(0, int(h * 0.20)):min(h, int(h * 0.80)), x0 + max(0, int((x1-x0)*0.15)):max(x0+1, x1-int((x1-x0)*0.15)), :]
            if tile.size == 0:
                continue
            med_rgb = np.median(tile.reshape(-1, 3), axis=0)
            sample = np.uint8([[med_rgb.astype(np.uint8)]])
            hue, satv, valv = [float(v) for v in np.asarray(Image.fromarray(sample, "RGB").convert("HSV"))[0, 0]]
            r, g, b = [float(v) for v in med_rgb]
            candidates = []
            if satv < 70 and valv > 80:
                candidates.append(("2X", max(0.0, 1.0 - satv / 85.0) * min(1.0, valv / 150.0)))
            if (hue >= 225 or hue <= 8) and satv >= 65:
                hue_dist = min(abs(hue - 245), abs(hue - 255), abs(hue - 0))
                candidates.append(("3X", min(1.0, satv / 180.0) * max(0.0, 1.0 - hue_dist / 55.0) * min(1.0, valv / 170.0)))
            if 72 <= hue <= 150 and satv >= 55:
                hue_dist = abs(hue - 112)
                candidates.append(("5X", min(1.0, satv / 170.0) * max(0.0, 1.0 - hue_dist / 65.0) * min(1.0, valv / 170.0)))
            if 12 <= hue <= 55 and satv >= 55:
                hue_dist = abs(hue - 30)
                candidates.append(("10X", min(1.0, satv / 170.0) * max(0.0, 1.0 - hue_dist / 35.0) * min(1.0, valv / 170.0)))
            if not candidates:
                continue
            candidates.sort(key=lambda item: item[1], reverse=True)
            mult, score = candidates[0]
            if score >= 0.25:
                qrgb = tuple(int(round(v / 8) * 8) for v in med_rgb)
                results.append({
                    "multiplier": mult,
                    "confidence": min(1.0, max(0.0, score)),
                    "signature": hashlib.sha256(f"{mult}:{qrgb}".encode()).hexdigest()[:20],
                    "rgb": qrgb,
                    "x": x0,
                })

        if not results:
            return [], "Ячейки не распознаны. Выделите только верхнюю полоску с цветными прямоугольниками."
        results.sort(key=lambda r: r["x"])
        return results, None

    def scan_once(self):
        if not self.region:
            messagebox.showinfo("Выберите область", "Сначала выделите всю полоску истории результатов.")
            return
        try:
            rgb = self._capture()
            self.last_capture = rgb
            self._show_preview(rgb)
            rows, error = self._analyze_image(rgb)
            if error:
                self.latest_detection = None
                self.status_var.set(error)
                return

            sequence = tuple(row["multiplier"] for row in rows)
            # Require the same detected sequence for three scans to avoid animation flicker.
            if sequence == self.pending_history_sequence:
                self.pending_history_count += 1
            else:
                self.pending_history_sequence = sequence
                self.pending_history_count = 1
            if self.pending_history_count < 3:
                self.status_var.set(f"Полоска распознана: {' → '.join(sequence)}. Проверяю стабильность…")
                return

            if self.last_history_sequence is None:
                self.last_history_sequence = sequence
                self.status_var.set(
                    f"Начальная история сохранена ({len(sequence)} ячеек). Ожидаю её обновления; старые ячейки не добавлены."
                )
                self._update_stats()
                return

            old = self.last_history_sequence
            if sequence == old:
                self.status_var.set(f"История не изменилась ({len(sequence)} ячеек). Новых записей нет.")
                self._update_stats()
                return

            # Find a rolling-strip shift. The strip can gain a new item on either end.
            candidates = []
            max_overlap = min(len(old), len(sequence))
            for overlap in range(max_overlap, 0, -1):
                if old[-overlap:] == sequence[:overlap]:
                    candidates.append(("left", overlap))
                    break
            for overlap in range(max_overlap, 0, -1):
                if old[:overlap] == sequence[-overlap:]:
                    candidates.append(("right", overlap))
                    break

            # Pick the shift with the greatest overlap. Only record the newly exposed tile(s).
            if candidates:
                side, overlap = max(candidates, key=lambda c: c[1])
                if side == "left":
                    new_mults = sequence[len(sequence) - (len(sequence) - overlap):] if False else sequence[overlap:]
                    # A fixed-width strip usually shifts left: the new item appears at the right.
                    # In that case old suffix == new prefix, so items after the overlap are new.
                    if not new_mults:
                        new_mults = (sequence[0],) if len(sequence) == len(old) else ()
                else:
                    new_mults = sequence[:max(0, len(sequence) - overlap)]
                    if not new_mults:
                        new_mults = (sequence[-1],) if len(sequence) == len(old) else ()
                # For same-length rolling history, the newly exposed tile is the unmatched end.
                if len(sequence) == len(old) and overlap == len(sequence) - 1:
                    new_mults = (sequence[-1],) if side == "left" else (sequence[0],)
                row_by_mult = {r["multiplier"]: r for r in rows}
                for mult in new_mults:
                    row = row_by_mult.get(mult)
                    if row:
                        self._record_round(row, verified="Авто")
                        if self.calibration_active:
                            self.calibration_count += 1
            else:
                self.status_var.set(
                    "Полоска изменилась, но надёжно сопоставить историю не удалось. Новые записи не добавлены — проверьте область."
                )
                self.last_history_sequence = sequence
                self._update_stats()
                return

            self.last_history_sequence = sequence
            if self.calibration_active:
                self.calibration_var.set(f"Калибровка: {self.calibration_count}/{self.calibration_target} новых элементов истории.")
                if self.calibration_count >= self.calibration_target:
                    self.calibration_active = False
                    self.next_round_capture = None
                    self.calibration_var.set(f"Калибровка завершена: обнаружено {self.calibration_count} новых элементов. Проверьте записи.")
                    self.status_var.set("Калибровка завершена. Проверьте автоматические записи.")
                    self.stop_monitoring()
                else:
                    self.status_var.set(f"История обновилась. Добавлено записей: {len(new_mults)}.")
            else:
                self.status_var.set(f"История обновилась. Добавлено записей: {len(new_mults)}.")
            self._update_stats()
        except Exception as exc:
            self.status_var.set(f"Ошибка: {exc}")

    def _record_round(self, row, verified="Авто"):
        # Unique per scheduled round, even when consecutive outcomes share the same colour.
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sig = hashlib.sha256(
            f"{stamp}|{time.time_ns()}|{row['multiplier']}".encode("utf-8")
        ).hexdigest()[:20]
        item = {
            "time": stamp,
            "multiplier": row["multiplier"],
            "confidence": f'{row["confidence"]*100:.0f}%',
            "verified": verified,
            "signature": sig,
        }
        self.history.insert(0, item)
        self.seen_signatures.add(sig)
        if self.calibration_active:
            self.calibration_session_signatures.add(sig)
        self.table.insert(
            "", 0, iid=sig,
            values=(item["time"], item["multiplier"], item["confidence"], item["verified"], sig)
        )
        self._save_csv()

    def start_calibration(self):
        if not self.region:
            messagebox.showinfo("Выбери область", "Сначала выдели всю полоску истории результатов.")
            return
        try:
            interval = float(self.interval_var.get().replace(",", "."))
            round_seconds = float(self.round_var.get().replace(",", "."))
            if not (0.15 <= interval <= 5):
                raise ValueError("Интервал сканирования должен быть от 0.15 до 5 секунд.")
            if not (5 <= round_seconds <= 120):
                raise ValueError("Длительность раунда должна быть от 5 до 120 секунд.")
        except ValueError as exc:
            messagebox.showerror("Неверные настройки", str(exc) or "Проверь интервалы.")
            return
        if self.running:
            self.stop_monitoring()
        self.calibration_active = True
        self.calibration_target = 50
        self.calibration_count = 0
        self.calibration_session_mode = True
        self.calibration_session_signatures = set()
        self.round_seconds = round_seconds
        self.next_round_capture = None
        self.last_history_sequence = None
        self.pending_history_sequence = None
        self.pending_history_count = 0
        self.calibration_var.set("Калибровка: 0/50 новых элементов истории. Сначала сохраняется текущая полоска как исходная.")
        self.start_monitoring()
        self.status_var.set(
            f"Калибровка запущена. Сканирование каждые {interval:g} сек.; запись только при обновлении полоски истории."
        )

    def _show_preview(self, rgb):
        img = Image.fromarray(rgb)
        img.thumbnail((850, 160))
        photo = ImageTk.PhotoImage(img)
        self.preview_label.configure(image=photo, text="")
        self.preview_label.image = photo

    def start_monitoring(self):
        if not self.region:
            messagebox.showinfo("Выберите область", "Сначала выделите всю полоску истории результатов.")
            return
        try:
            interval = float(self.interval_var.get().replace(",", "."))
            round_seconds = float(self.round_var.get().replace(",", "."))
            if interval < 0.15 or interval > 5:
                raise ValueError
            if round_seconds < 5 or round_seconds > 120:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Неверный интервал",
                "Сканирование: 0.15–5 секунд. Длительность раунда: 5–120 секунд."
            )
            return
        if self.running:
            return
        self.round_seconds = round_seconds
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"Мониторинг полоски истории запущен: проверка каждые {interval:g} сек.")
        self.worker = threading.Thread(target=self._monitor_loop, args=(interval,), daemon=True)
        self.worker.start()

    def _monitor_loop(self, interval):
        while self.running:
            self.events.put(("scan", None))
            time.sleep(interval)

    def stop_monitoring(self):
        self.running = False
        if getattr(self, "calibration_active", False):
            self.calibration_active = False
            self.next_round_capture = None
            if hasattr(self, "calibration_var"):
                self.calibration_var.set(f"Калибровка остановлена на {self.calibration_count}/{self.calibration_target}.")
        if hasattr(self, "start_btn"):
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        if hasattr(self, "status_var") and self.region:
            self.status_var.set("Мониторинг остановлен.")

    def _drain_queue(self):
        try:
            while True:
                kind, _ = self.events.get_nowait()
                if kind == "scan" and self.running:
                    self.scan_once()
        except queue.Empty:
            pass
        self.after(200, self._drain_queue)

    def _save_csv(self):
        fields = ["time", "multiplier", "confidence", "verified", "signature"]
        with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for item in reversed(self.history):
                writer.writerow({k: item.get(k, "Нет" if k == "verified" else "") for k in fields})

    def _load_existing_csv(self):
        if not os.path.exists(self.csv_path):
            self._update_stats()
            return
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if not row.get("signature"):
                        continue
                    row.setdefault("verified", "Нет")
                    # Avoid collisions if a CSV contains duplicate signatures.
                    iid = row["signature"]
                    if self.table.exists(iid):
                        iid = f'{iid}_{len(self.history)}'
                        row["signature"] = iid
                    self.seen_signatures.add(row["signature"])
                    self.history.insert(0, row)
                    self.table.insert("", "end", iid=iid, values=(
                        row.get("time", ""), row.get("multiplier", ""),
                        row.get("confidence", ""), row.get("verified", "Нет"),
                        row.get("signature", "")
                    ))
            self._update_stats()
        except Exception as exc:
            self.status_var.set(f"Не удалось прочитать старую историю: {exc}")

    def _selected_item(self):
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo("Выбери строку", "Сначала выбери строку в таблице.")
            return None, None
        iid = selected[0]
        values = self.table.item(iid, "values")
        signature = str(values[4]) if len(values) > 4 else iid
        item = next((x for x in self.history if str(x.get("signature", "")) == signature), None)
        if item is None:
            item = next((x for x in self.history if str(x.get("signature", "")) == iid), None)
        return iid, item

    def confirm_selected(self):
        iid, item = self._selected_item()
        if not item:
            return
        item["verified"] = "Да"
        self._refresh_row(iid, item)
        self._save_csv()
        self._update_stats()
        self.status_var.set("Результат подтверждён и включён в статистику.")

    def edit_selected(self):
        iid, item = self._selected_item()
        if not item:
            return
        win = tk.Toplevel(self)
        win.title("Исправить результат")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="Выбери фактический результат в истории игры:").pack(padx=16, pady=(16, 8))
        var = tk.StringVar(value=item.get("multiplier", "2X"))
        combo = ttk.Combobox(win, textvariable=var, values=MULTIPLIERS, state="readonly", width=12)
        combo.pack(padx=16, pady=4)

        def save():
            if var.get() not in MULTIPLIERS:
                return
            item["multiplier"] = var.get()
            item["verified"] = "Да"
            self._refresh_row(iid, item)
            self._save_csv()
            self._update_stats()
            self.status_var.set("Результат исправлен и подтверждён.")
            win.destroy()

        ttk.Button(win, text="Сохранить и подтвердить", command=save).pack(padx=16, pady=16)

    def _refresh_row(self, iid, item):
        if self.table.exists(iid):
            self.table.item(iid, values=(
                item.get("time", ""), item.get("multiplier", ""),
                item.get("confidence", ""), item.get("verified", "Нет"),
                item.get("signature", "")
            ))

    def _update_stats(self):
        if self.calibration_session_mode:
            session_rows = [x for x in self.history
                            if x.get("signature") in self.calibration_session_signatures]
            verified = [x for x in session_rows
                        if x.get("verified") in ("Да", "Авто") and x.get("multiplier") in MULTIPLIERS]
        else:
            verified = [x for x in self.history
                        if x.get("verified") in ("Да", "Авто") and x.get("multiplier") in MULTIPLIERS]
        recent = verified[:50]
        n = len(recent)
        self.stats_var.set(
            f"Результатов в расчёте: {len(verified)}. В расчёте последние {n} записей (автоматические + исправленные вручную). "
            "Автоматические записи нужно проверить: ошибка цвета исказит проценты."
        )

        counts = {m: sum(1 for x in recent if x.get("multiplier") == m) for m in MULTIPLIERS}
        probs = {}
        for mult in MULTIPLIERS:
            # Empirical frequency, not smoothed: the user should see what was actually observed.
            pct = (counts[mult] / n * 100.0) if n else 0.0
            probs[mult] = pct
            multiplier = int(mult[:-1])
            breakeven = 100.0 / multiplier
            edge = pct / 100.0 * multiplier - 1.0 if n else None
            edge_text = f", EV≈{edge*100:+.1f}%" if edge is not None else ""
            self.prob_vars[mult].set(
                f"{mult}: {pct:.1f}% ({counts[mult]}/{n}), безуб. {breakeven:.1f}%{edge_text}"
            )

        # Show a historical-frequency estimate only after at least 10 usable results.
        # This is descriptive statistics, not a reliable prediction of random outcomes.
        if n < 10:
            remaining = 10 - n
            self.recommendation_var.set(
                f"Оценка появится после 10 результатов. Сейчас учтено: {n}/10; "
                f"нужно ещё {remaining}. Проценты не будут показываться раньше порога."
            )
        else:
            smoothed = {m: ((counts[m] + 1) / (n + len(MULTIPLIERS)) * 100.0)
                        for m in MULTIPLIERS}
            top = max(MULTIPLIERS, key=lambda m: smoothed[m])
            forecast_text = " | ".join(f"{m}: {smoothed[m]:.1f}%" for m in MULTIPLIERS)
            self.recommendation_var.set(
                f"Оценка по последним {n} результатам (сглаженная): {forecast_text}. "
                f"Чаще всего в выборке: {top}. Данные обновляются автоматически при добавлении "
                "результатов; это историческая оценка, не надёжное предсказание случайного раунда."
            )

    def export_csv(self):
        target = filedialog.asksaveasfilename(
            title="Сохранить историю", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], initialfile="roulette_history_export.csv"
        )
        if not target:
            return
        try:
            with open(target, "w", newline="", encoding="utf-8-sig") as f:
                fields = ["time", "multiplier", "confidence", "verified", "signature"]
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for item in reversed(self.history):
                    writer.writerow({k: item.get(k, "Нет" if k == "verified" else "") for k in fields})
            self.status_var.set(f"Экспортировано: {target}")
        except Exception as exc:
            messagebox.showerror("Ошибка экспорта", str(exc))

    def on_close(self):
        self.stop_monitoring()
        self.destroy()


if __name__ == "__main__":
    app = RouletteAnalyzer()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
