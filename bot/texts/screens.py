"""Unified screen text builder for consistent UI across all bot screens.

Usage:
    text = (
        Screen(E.ROCKET, "PVNDORA")
        .blank()
        .line(f"{E.WORDPRESS} site.com")
        .blank()
        .field(E.FOLDER, "Категорий", "2")
        .field(E.ANALYTICS, "Публикаций", "16")
        .hint("Управляйте проектом и контентом")
        .build()
    )
"""

from __future__ import annotations

from bot.texts.emoji import E

# Separator line used between sections
SEPARATOR = "\u2500" * 10


class Screen:
    """Fluent builder for bot screen text (parse_mode=HTML)."""

    def __init__(self, icon: str, title: str) -> None:
        self._lines: list[str] = [f"{icon} <b>{title}</b>"]

    def blank(self) -> Screen:
        self._lines.append("")
        return self

    def line(self, text: str) -> Screen:
        self._lines.append(text)
        return self

    def field(self, icon: str, label: str, value: str | int) -> Screen:
        self._lines.append(f"{icon} {label}: {value}")
        return self

    def field_if(self, icon: str, label: str, value: str | None, *, max_len: int = 60) -> Screen:
        """Add field only if value is truthy. Truncate long values."""
        if not value:
            return self
        display = value[:max_len] + "\u2026" if len(value) > max_len else value
        self._lines.append(f"{icon} {label}: {display}")
        return self

    def check(self, label: str, *, ok: bool, detail: str = "") -> Screen:
        icon = E.CHECK if ok else E.CLOSE
        suffix = f" \u2014 {detail}" if detail else ""
        self._lines.append(f"{icon} {label}{suffix}")
        return self

    def section(self, icon: str, title: str) -> Screen:
        self._lines.append("")
        self._lines.append(f"{icon} <b>{title}</b>")
        return self

    def hint(self, text: str) -> Screen:
        self._lines.append("")
        self._lines.append(SEPARATOR)
        self._lines.append(f"{E.LIGHTBULB} <i>{text}</i>")
        return self

    def separator(self) -> Screen:
        self._lines.append("")
        self._lines.append(SEPARATOR)
        return self

    def progress(self, step: int, total: int, label: str = "") -> Screen:
        """Prepend a progress indicator: 'Этап N из M · label' + dots.

        Presentation only (guided-flow PR-1). Inserted above the title so it
        appears at the very top of the screen.
        """
        dots = "\u25cf" * max(0, step) + "\u25cb" * max(0, total - step)
        head = f"Этап {step} из {total}"
        if label:
            head += f" \u00b7 {label}"
        self._lines.insert(0, "")
        self._lines.insert(0, dots)
        self._lines.insert(0, f"<i>{head}</i>")
        return self

    def build(self) -> str:
        return "\n".join(self._lines)
