from __future__ import annotations

import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font
from typing import Any, Callable

HistoryPoint = tuple[float, float]

_mono: font.Font | tuple[str, int] = ("Courier", 9)
_mono_sm: font.Font | tuple[str, int] = ("Courier", 8)
_bold_hd: font.Font | tuple[str, int, str] = ("Courier", 10, "bold")
_mono_xs: font.Font | tuple[str, int] = ("Courier", 7)


@dataclass(frozen=True)
class RenderContext:
    width: int
    height: int
    pad_left: int
    pad_right: int
    pad_top: int
    pad_bottom: int
    plot_width: int
    plot_height: int
    visible: list[HistoryPoint]
    timestamps: list[float]
    values: list[float]
    low: float
    high: float
    time_span: float
    x_from_ts: Callable[[float], float]
    y_from_value: Callable[[float], float]


def init_chart_fonts(
    *,
    mono: font.Font,
    mono_sm: font.Font,
    bold_hd: font.Font,
    mono_xs: font.Font,
) -> None:
    global _mono, _mono_sm, _bold_hd, _mono_xs
    _mono = mono
    _mono_sm = mono_sm
    _bold_hd = bold_hd
    _mono_xs = mono_xs


class BaseChart(tk.Canvas):
    PAD_L: int = 64
    PAD_R: int = 20
    PAD_T: int = 20
    PAD_B: int = 40
    MIN_VISIBLE_POINTS: int = 4

    def __init__(self, parent: tk.Misc, bg: str, hl: str, **kwargs: Any) -> None:
        super().__init__(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=hl,
            takefocus=1,
            **kwargs,
        )
        self._history: list[HistoryPoint] = []
        self._view_start: int = 0
        self._view_size: int = 0
        self._selector_index: int = 0
        self._last_len: int = 0
        self._last_val: float = 0.0
        self._baseline_value: float = 0.0
        self._dirty: bool = False
        self._drag_x: int | None = None
        self._drag_view_start: int = 0
        self.bind("<Configure>", lambda _event: self._mark_dirty())
        self.bind("<MouseWheel>", self._on_scroll)
        self.bind("<Motion>", self._on_motion)
        self.bind("<ButtonPress-1>", self._drag_start)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", self._drag_end)
        self.bind("<Left>", self._on_left)
        self.bind("<Right>", self._on_right)
        self.bind("<Enter>", lambda _event: self.focus_set())

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _visible_count(self) -> int:
        count = len(self._history)
        if count == 0:
            return 0
        if self._view_size <= 0 or self._view_size > count:
            return count
        return max(min(self.MIN_VISIBLE_POINTS, count), self._view_size)

    def _clamp_state(self) -> None:
        count = len(self._history)
        if count == 0:
            self._view_start = 0
            self._view_size = 0
            self._selector_index = 0
            return
        self._selector_index = max(0, min(self._selector_index, count - 1))
        visible_count = self._visible_count()
        self._view_size = visible_count
        self._view_start = max(0, min(self._view_start, count - visible_count))

    def _reset_view(self) -> None:
        self._view_start = 0
        self._view_size = len(self._history)
        self._selector_index = max(0, len(self._history) - 1)
        self._clamp_state()

    def _keep_selector_visible(self) -> None:
        self._clamp_state()
        visible_count = self._visible_count()
        if visible_count == 0:
            return
        if self._selector_index < self._view_start:
            self._view_start = self._selector_index
        elif self._selector_index >= self._view_start + visible_count:
            self._view_start = self._selector_index - visible_count + 1
        self._clamp_state()

    def _center_view_on_selector(self) -> None:
        self._clamp_state()
        visible_count = self._visible_count()
        if visible_count == 0:
            return
        self._view_start = self._selector_index - (visible_count // 2)
        self._clamp_state()

    def _visible(self) -> list[HistoryPoint]:
        self._clamp_state()
        visible_count = self._visible_count()
        return self._history[self._view_start:self._view_start + visible_count]

    def _selector_visible_index(self) -> int | None:
        visible_count = self._visible_count()
        if visible_count == 0:
            return None
        visible_index = self._selector_index - self._view_start
        if 0 <= visible_index < visible_count:
            return visible_index
        return None

    def _selector_index_from_x(self, x_pos: int) -> int | None:
        visible = self._visible()
        if not visible:
            return None
        plot_width = (self.winfo_width() or 600) - self.PAD_L - self.PAD_R
        if plot_width <= 0:
            return None
        relative_x = max(0, min(plot_width, x_pos - self.PAD_L))
        visible_index = int(round(relative_x / max(plot_width, 1) * max(len(visible) - 1, 0)))
        return self._view_start + visible_index

    def _move_selector(self, step: int) -> None:
        if not self._history:
            return
        self._selector_index = max(0, min(self._selector_index + step, len(self._history) - 1))
        self._keep_selector_visible()
        self._redraw()

    def _on_left(self, _event: tk.Event[tk.Misc]) -> str:
        self._move_selector(-1)
        return "break"

    def _on_right(self, _event: tk.Event[tk.Misc]) -> str:
        self._move_selector(1)
        return "break"

    def _on_scroll(self, event: tk.Event[tk.Misc]) -> None:
        count = len(self._history)
        if count < 2:
            return
        visible_count = self._visible_count()
        step = max(1, visible_count // 5)
        delta = int(getattr(event, "delta", 0))
        if delta > 0:
            self._view_size = max(min(self.MIN_VISIBLE_POINTS, count), visible_count - step)
        else:
            self._view_size = min(count, visible_count + step)
        self._center_view_on_selector()
        self._redraw()

    def _drag_start(self, event: tk.Event[tk.Misc]) -> None:
        self.focus_set()
        x_pos = int(getattr(event, "x", 0))
        selector_index = self._selector_index_from_x(x_pos)
        if selector_index is not None:
            self._selector_index = selector_index
        self._keep_selector_visible()
        self._drag_x = x_pos
        self._drag_view_start = self._view_start
        self._redraw()

    def _drag_move(self, event: tk.Event[tk.Misc]) -> None:
        if self._drag_x is None:
            return
        count = len(self._history)
        if count < 2:
            return
        plot_width = (self.winfo_width() or 600) - self.PAD_L - self.PAD_R
        if plot_width <= 0:
            return
        visible_count = self._visible_count()
        if visible_count >= count:
            return
        px_per_point = plot_width / max(visible_count - 1, 1)
        x_pos = int(getattr(event, "x", 0))
        delta_points = int(round((self._drag_x - x_pos) / max(px_per_point, 1)))
        new_start = max(0, min(self._drag_view_start + delta_points, count - visible_count))
        if new_start != self._view_start:
            self._view_start = new_start
            self._redraw()

    def _drag_end(self, _event: tk.Event[tk.Misc]) -> None:
        self._drag_x = None

    def _on_motion(self, event: tk.Event[tk.Misc]) -> None:
        if self._drag_x is not None:
            return
        x_pos = int(getattr(event, "x", 0))
        selector_index = self._selector_index_from_x(x_pos)
        if selector_index is None or selector_index == self._selector_index:
            return
        self._selector_index = selector_index
        self._keep_selector_visible()
        self._redraw()

    def _crosshair_label(self, ts: float, value: float) -> str:
        diff = value - self._baseline_value
        return f"{time.strftime('%m/%d %H:%M', time.localtime(ts))}  ${value:.4f}  ({diff:+.4f})"

    def _crosshair_color(self, value: float) -> str:
        return "#00ff55" if value >= self._baseline_value else "#ff5555"

    def _is_at_or_above_baseline(self, value: float) -> bool:
        return value >= self._baseline_value

    def _apply_loaded_history(
        self,
        *,
        history: list[HistoryPoint],
        new_len: int,
        new_last: float,
        reset_view: bool,
        track_latest: bool,
    ) -> None:
        self._history = history
        self._last_len = new_len
        self._last_val = new_last
        if reset_view:
            self._reset_view()
        elif track_latest and self._history:
            self._selector_index = len(self._history) - 1
            self._keep_selector_visible()
        else:
            self._clamp_state()
        self._dirty = True

    def _begin_redraw(
        self,
    ) -> tuple[int, int, int, int, int, int, list[HistoryPoint]]:
        self.delete("chart")
        self.delete("crosshair")
        self.update_idletasks()
        width = self.winfo_width() or 600
        height = self.winfo_height() or 220
        visible = self._visible()
        return width, height, self.PAD_L, self.PAD_R, self.PAD_T, self.PAD_B, visible

    def _draw_empty_message(self, width: int, height: int, message: str) -> None:
        self.create_text(
            width // 2,
            height // 2,
            text=message,
            fill="#334455",
            font=_mono,
            tags="chart",
        )

    def _empty_message_text(self, visible: list[HistoryPoint]) -> str | None:
        if not visible:
            return "No data yet"
        return None
    
    def _compute_y_bounds(self, values: list[float]) -> tuple[float, float]:
        low, high = min(values), max(values)
        spread = max(high - low, 0.5)
        return low - spread * 0.1, high + spread * 0.1

    # def _compute_y_bounds(self, values: list[float]) -> tuple[float, float]:
    #     low, high = min(values), max(values)
    #     if high == low:
    #         high += 0.005
    #         low -= 0.005
    #     return low, high

    def _build_context(
        self,
        *,
        width: int,
        height: int,
        pad_left: int,
        pad_right: int,
        pad_top: int,
        pad_bottom: int,
        visible: list[HistoryPoint],
    ) -> RenderContext:
        plot_width = width - pad_left - pad_right
        plot_height = height - pad_top - pad_bottom
        timestamps = [ts for ts, _ in visible]
        values = [value for _, value in visible]
        low, high = self._compute_y_bounds(values)
        time_min, time_max = timestamps[0], timestamps[-1]
        time_span = max(time_max - time_min, 1.0)

        def x_from_ts(ts: float) -> float:
            return pad_left + ((ts - time_min) / time_span) * plot_width

        def y_from_value(value: float) -> float:
            return pad_top + (1 - (value - low) / (high - low)) * plot_height

        return RenderContext(
            width=width,
            height=height,
            pad_left=pad_left,
            pad_right=pad_right,
            pad_top=pad_top,
            pad_bottom=pad_bottom,
            plot_width=plot_width,
            plot_height=plot_height,
            visible=visible,
            timestamps=timestamps,
            values=values,
            low=low,
            high=high,
            time_span=time_span,
            x_from_ts=x_from_ts,
            y_from_value=y_from_value,
        )

    def _guide_values(self, ctx: RenderContext) -> list[float]:
        return [ctx.low + (ctx.high - ctx.low) * index / 4 for index in range(5)]

    def _guide_line_color(self) -> str:
        return "#0d142a"

    def _guide_text_color(self) -> str:
        return "#334455"

    def _guide_label(self, value: float) -> str:
        return f"{value:.4f}"

    def _draw_guides(self, ctx: RenderContext) -> None:
        for guide_value in self._guide_values(ctx):
            guide_y = ctx.y_from_value(guide_value)
            self.create_line(
                ctx.pad_left,
                guide_y,
                ctx.width - ctx.pad_right,
                guide_y,
                fill=self._guide_line_color(),
                dash=(2, 4),
                tags="chart",
            )
            self.create_text(
                ctx.pad_left - 4,
                guide_y,
                text=self._guide_label(guide_value),
                fill=self._guide_text_color(),
                anchor="e",
                font=_mono_xs,
                tags="chart",
            )

    def _baseline_text(self) -> str | None:
        return f"{self._baseline_value:.4f}"

    def _baseline_line_color(self) -> str:
        return "#665500"

    def _baseline_text_color(self) -> str:
        return "#998833"

    def _draw_baseline(self, ctx: RenderContext) -> None:
        baseline_text = self._baseline_text()
        if baseline_text is None:
            return
        baseline_y = ctx.y_from_value(self._baseline_value)
        if not (ctx.pad_top < baseline_y < ctx.height - ctx.pad_bottom):
            return
        self.create_line(
            ctx.pad_left,
            baseline_y,
            ctx.width - ctx.pad_right,
            baseline_y,
            fill=self._baseline_line_color(),
            dash=(4, 4),
            tags="chart",
        )
        self.create_text(
            ctx.pad_left - 4,
            baseline_y,
            text=baseline_text,
            fill=self._baseline_text_color(),
            anchor="e",
            font=_mono_xs,
            tags="chart",
        )

    def _time_tick_count(self, visible: list[HistoryPoint]) -> int:
        return min(6, len(visible))

    def _draw_time_axis(self, ctx: RenderContext) -> None:
        time_format = "%H:%M" if (ctx.time_span / 3600) < 20 else "%m/%d %H:%M"
        label_count = self._time_tick_count(ctx.visible)
        for label_index in range(label_count):
            visible_index = int(label_index / max(label_count - 1, 1) * (len(ctx.visible) - 1))
            tick_x = ctx.x_from_ts(ctx.timestamps[visible_index])
            tick_y = ctx.height - ctx.pad_bottom
            self.create_line(tick_x, tick_y, tick_x, tick_y + 4, fill=self._guide_text_color(), tags="chart")
            self.create_text(
                tick_x,
                tick_y + 12,
                text=time.strftime(time_format, time.localtime(ctx.timestamps[visible_index])),
                fill=self._guide_text_color(),
                font=_mono_xs,
                tags="chart",
            )

    def _draw_series(self, ctx: RenderContext) -> None:
        raise NotImplementedError

    def _draw_header(self, ctx: RenderContext) -> None:
        return None

    def _draw_selector_overlay(
        self,
        *,
        visible: list[HistoryPoint],
        x_pos: float,
        y_pos: float,
        plot_left: int,
        plot_right: int,
        plot_top: int,
        plot_bottom: int,
        width: int,
        height: int,
    ) -> None:
        selector_visible_index = self._selector_visible_index()
        if selector_visible_index is None:
            return
        selector_ts, selector_value = visible[selector_visible_index]
        self.create_line(x_pos, plot_top, x_pos, height - plot_bottom, fill="#556677", dash=(4, 4), tags="crosshair")
        self.create_line(plot_left, y_pos, width - plot_right, y_pos, fill="#334455", dash=(2, 4), tags="crosshair")
        selector_label = self._crosshair_label(selector_ts, selector_value)
        selector_color = self._crosshair_color(selector_value)
        box_width = len(selector_label) * 7 + 8
        label_x = min(x_pos + 6, width - box_width - 4)
        self.create_rectangle(label_x, y_pos - 10, label_x + box_width, y_pos + 10, fill="#0d1a2a", outline="#1a3a5a", tags="crosshair")
        self.create_text(label_x + 4, y_pos, text=selector_label, fill=selector_color, font=_mono_sm, anchor="w", tags="crosshair")

    def _draw_footer(self, width: int, height: int, visible_count: int) -> None:
        self.create_text(
            width // 2,
            height - 8,
            text=f"↔ {visible_count}/{len(self._history)} pts | Scroll=zoom on selector | ← → move | Drag=pan",
            fill="#334455",
            font=_mono_xs,
            tags="chart",
        )

    def _redraw(self) -> None:
        width, height, pad_left, pad_right, pad_top, pad_bottom, visible = self._begin_redraw()
        empty_message = self._empty_message_text(visible)
        if empty_message is not None:
            self._draw_empty_message(width, height, empty_message)
            return

        ctx = self._build_context(
            width=width,
            height=height,
            pad_left=pad_left,
            pad_right=pad_right,
            pad_top=pad_top,
            pad_bottom=pad_bottom,
            visible=visible,
        )
        self._draw_guides(ctx)
        self._draw_baseline(ctx)
        self._draw_time_axis(ctx)
        self._draw_series(ctx)

        selector_visible_index = self._selector_visible_index()
        if selector_visible_index is not None:
            self._draw_selector_overlay(
                visible=ctx.visible,
                x_pos=ctx.x_from_ts(ctx.timestamps[selector_visible_index]),
                y_pos=ctx.y_from_value(ctx.values[selector_visible_index]),
                plot_left=ctx.pad_left,
                plot_right=ctx.pad_right,
                plot_top=ctx.pad_top,
                plot_bottom=ctx.pad_bottom,
                width=ctx.width,
                height=ctx.height,
            )

        self._draw_header(ctx)
        self._draw_footer(width, height, len(visible))


class PositionChart(BaseChart):
    PAD_L, PAD_R, PAD_T, PAD_B = 60, 20, 28, 40

    def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
        super().__init__(parent, bg="#050510", hl="#1a2a4a", **kwargs)
        self._title: str = ""
        self._empty_message: str = "Select a position to view its live price chart"

    def load(
        self,
        history: list[HistoryPoint] | None,
        title: str,
        entry_price: float,
        empty_message: str | None = None,
    ) -> None:
        new_len = len(history) if history else 0
        new_msg = empty_message or "Select a position to view its live price chart"
        new_last = history[-1][1] if history else 0.0
        track_latest = self._selector_index >= max(self._last_len - 1, 0)
        position_changed = title != self._title or entry_price != self._baseline_value
        data_changed = (
            new_len != self._last_len
            or new_last != self._last_val
            or new_msg != self._empty_message
        )
        if position_changed or data_changed:
            history_points = list(history) if history else []
            self._title = title
            self._baseline_value = entry_price
            self._empty_message = new_msg
            self._apply_loaded_history(
                history=history_points,
                new_len=new_len,
                new_last=new_last,
                reset_view=position_changed,
                track_latest=track_latest,
            )
        if self._dirty:
            self._redraw()
            self._dirty = False

    def _crosshair_label(self, ts: float, value: float) -> str:
        pct = (value - self._baseline_value) / max(self._baseline_value, 0.001) * 100
        return f"${value:.4f}  ({pct:+.1f}%)"


    def _draw_series(self, ctx: RenderContext) -> None:
        if len(ctx.values) >= 2:
            polygon_coords: list[float] = [ctx.pad_left, ctx.height - ctx.pad_bottom]
            for ts, price in zip(ctx.timestamps, ctx.values):
                polygon_coords.extend([ctx.x_from_ts(ts), ctx.y_from_value(price)])
            polygon_coords.extend([ctx.x_from_ts(ctx.timestamps[-1]), ctx.height - ctx.pad_bottom])
            self.create_polygon(
                polygon_coords,
                fill="#001a0d" if self._is_at_or_above_baseline(ctx.values[-1]) else "#1a0000",
                outline="",
                smooth=False,
                tags="chart",
            )

        line_coords: list[float] = []
        for ts, price in zip(ctx.timestamps, ctx.values):
            line_coords.extend([ctx.x_from_ts(ts), ctx.y_from_value(price)])
        if len(line_coords) >= 4:
            current_price = ctx.values[-1]
            self.create_line(
                line_coords,
                fill="#00ff88" if self._is_at_or_above_baseline(current_price) else "#ff5555",
                width=2,
                smooth=len(ctx.values) >= 6,
                tags="chart",
            )

        buy_x = ctx.x_from_ts(ctx.timestamps[0])
        buy_y = ctx.y_from_value(ctx.values[0])
        self.create_text(buy_x, buy_y - 14, text="▲ BUY", fill="#ffdd00", font=_mono_sm, tags="chart")
        self.create_oval(buy_x - 4, buy_y - 4, buy_x + 4, buy_y + 4, fill="#ffdd00", outline="", tags="chart")

        current_price = ctx.values[-1]
        end_x = ctx.x_from_ts(ctx.timestamps[-1])
        end_y = ctx.y_from_value(current_price)
        dot_color = "#00ff88" if self._is_at_or_above_baseline(current_price) else "#ff5555"
        self.create_oval(end_x - 5, end_y - 5, end_x + 5, end_y + 5, fill=dot_color, outline="#ffffff", width=1, tags="chart")

    def _draw_header(self, ctx: RenderContext) -> None:
        current_price = ctx.values[-1]
        pct = (current_price - self._baseline_value) / max(self._baseline_value, 0.001) * 100
        self.create_text(ctx.pad_left, ctx.pad_top, text=f"📈 {self._title[:60]}", fill="#00ff88", anchor="w", font=_bold_hd, tags="chart")
        self.create_text(
            ctx.width - ctx.pad_right,
            ctx.pad_top,
            text=f"Now ${current_price:.4f}  ({pct:+.1f}%)   Entry ${self._baseline_value:.4f}",
            fill="#00ff55" if pct >= 0 else "#ff5555",
            anchor="e",
            font=_mono_sm,
            tags="chart",
        )


class PnLChart(BaseChart):
    PAD_L, PAD_R, PAD_T, PAD_B = 64, 20, 20, 40

    def __init__(self, parent: tk.Misc, **kwargs: Any) -> None:
        super().__init__(parent, bg="#06060f", hl="#1a1a30", **kwargs)

    def load(self, history: list[HistoryPoint], bankroll_start: float) -> None:
        new_len = len(history)
        new_last = history[-1][1] if history else 0.0
        track_latest = self._selector_index >= max(self._last_len - 1, 0)
        if new_len != self._last_len or new_last != self._last_val or bankroll_start != self._baseline_value:
            self._baseline_value = bankroll_start
            self._apply_loaded_history(
                history=list(history),
                new_len=new_len,
                new_last=new_last,
                reset_view=self._view_size == 0,
                track_latest=track_latest,
            )
        if self._dirty:
            self._redraw()
            self._dirty = False

    # def _empty_message_text(self, visible: list[HistoryPoint]) -> str | None:
    #     if len(visible) < 2:
    #         return "No data yet — graph appears after first equity point"
    #     return None

    def _guide_values(self, ctx: RenderContext) -> list[float]:
        return [ctx.low + (ctx.high - ctx.low) * i / 6 for i in range(7)]

    # def _guide_line_color(self) -> str:
    #     return "#1a1a28"

    # def _guide_text_color(self) -> str:
    #     return "#335544"

    def _guide_label(self, value: float) -> str:
        return f"${value:.3f}"

    # def _baseline_text(self) -> str | None:
    #     return "START"

    def _draw_series(self, ctx: RenderContext) -> None:
        polygon_coords: list[float] = [ctx.pad_left, ctx.y_from_value(ctx.values[0])]
        for ts, value in zip(ctx.timestamps, ctx.values):
            polygon_coords.extend([ctx.x_from_ts(ts), ctx.y_from_value(value)])
        polygon_coords.extend([ctx.x_from_ts(ctx.timestamps[-1]), ctx.height - ctx.pad_bottom, ctx.pad_left, ctx.height - ctx.pad_bottom])
        self.create_polygon(
            polygon_coords,
            fill="#001a0a" if self._is_at_or_above_baseline(ctx.values[-1]) else "#1a0000",
            outline="", smooth=False, tags="chart",
        )
        for i in range(1, len(ctx.values)):
            self.create_line(
                ctx.x_from_ts(ctx.timestamps[i - 1]), ctx.y_from_value(ctx.values[i - 1]),
                ctx.x_from_ts(ctx.timestamps[i]),     ctx.y_from_value(ctx.values[i]),
                fill="#00ff55" if ctx.values[i] >= ctx.values[i - 1] else "#ff5555",
                width=2, tags="chart",
            )
        self.create_oval(
            ctx.pad_left - 3, ctx.y_from_value(ctx.values[0]) - 3,
            ctx.pad_left + 3, ctx.y_from_value(ctx.values[0]) + 3,
            fill="#aaaaaa", outline="", tags="chart",
        )
        last_value = ctx.values[-1]
        end_x = ctx.x_from_ts(ctx.timestamps[-1])
        end_y = ctx.y_from_value(last_value)
        cur_color = "#00ff55" if self._is_at_or_above_baseline(last_value) else "#ff5555"
        self.create_oval(end_x - 4, end_y - 4, end_x + 4, end_y + 4, fill=cur_color, outline="#ffffff", tags="chart")
        diff = last_value - self._baseline_value
        self.create_text(
            min(end_x + 8, ctx.width - 220), end_y,
            text=f"${last_value:.3f} ({diff:+.3f})",
            fill=cur_color, font=("Courier", 9), anchor="w", tags="chart",
        )


__all__ = ["BaseChart", "PositionChart", "PnLChart", "init_chart_fonts"]
