"""PyInstaller 入口：以包模块方式启动应用。"""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
