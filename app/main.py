"""程序入口：音乐整理工具（Windows / PySide6 Widgets / 免安装便携版）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .paths import DataPaths
    from .services import AppService
    from .ui.icons import multi_color_icon
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("音乐整理工具")
    app.setOrganizationName("MusicOrganizer")
    app.setWindowIcon(multi_color_icon())
    app.setQuitOnLastWindowClosed(True)

    paths = DataPaths()
    service = AppService(paths)

    smoke = os.environ.get("MUSIC_ORG_SMOKE") == "1"
    window = MainWindow(service)
    window.show()
    if smoke:
        # 冒烟模式：禁用模态弹窗，截图后自动退出
        from PySide6.QtCore import QTimer
        MainWindow._show_notes = lambda self, title, notes: print(f"[notes] {title}")
        MainWindow._startup_notes = lambda self: None
        shots_dir = Path(os.environ.get("MUSIC_ORG_SMOKE_OUT", ""))

        def _grab(name: str = "") -> str:
            """截图。MUSIC_ORG_SMOKE_OUT 指定文件时直接用；指定目录时按
            name（场景注入的多张截图）或目录默认名落盘。"""
            if shots_dir.suffix == ".png" and not name:
                out = str(shots_dir)
            else:
                base = shots_dir if shots_dir.suffix != ".png" else shots_dir.parent
                out = str(base / f"{name or 'smoke'}.png")
            if out and out not in ("", ".png"):
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                from PySide6.QtWidgets import QApplication
                active = QApplication.activeWindow()
                target = active if active is not None and active is not window else window
                target.grab().save(out)
                print("smoke shot saved:", out, "|", type(target).__name__)
            return out

        def _grab_and_quit() -> None:
            _grab()
            QApplication.quit()

        run_script = os.environ.get("MUSIC_ORG_SMOKE_RUN", "")
        if run_script and Path(run_script).is_file():
            # 场景注入（仅冒烟模式）：脚本需定义 run(window, service, grab)，
            # 由场景自管截图与退出时机；无注入时保持 3 秒截图退出
            namespace: dict = {}
            exec(Path(run_script).read_text(encoding="utf-8"), namespace)  # noqa: S102
            namespace["run"](window, service, _grab)
        else:
            QTimer.singleShot(3000, _grab_and_quit)
    elif not paths.portable:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            window, "数据目录提示",
            f"程序目录不可写，设置与历史将保存到：\n{paths.root}\n\n"
            "便携数据目录 data/ 不可写时会自动回退，不会静默丢失历史。")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
