# -*- coding: utf-8 -*-
"""
打包 GitHub Releases 用的兩個壓縮檔（只用 Python 標準函式庫）：
  dist/twflow-windows-<版本>.zip   Windows 版（.bat）
  dist/twflow-mac-<版本>.zip       Mac 版（.command、mac_install.sh、launchd）
  dist/RELEASE_NOTES.md           Release 說明（安裝／更新步驟）

用法（在 twgoo 資料夾）：python release/build_release.py v2026.10.29
GitHub Actions 的 Release 流程（.github/workflows/release.yml）會自動執行這個檔案。

注意：壓縮檔裡放的是 config.default.json、sectors.default.csv，不放 config.json、sectors.csv，
這樣更新時解壓縮覆蓋，不會蓋掉使用者改過的設定（fetch.py 第一次執行時會自己複製一份）。
"""
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

SHARED = ["fetch.py", "live.py", "dashboard.html", "使用說明.txt"]
DEFAULTS = {"config.json": "config.default.json", "sectors.csv": "sectors.default.csv"}
WINDOWS = ["run.bat", "live.bat", "stop_live.bat", "setup_task.bat", "run_task.bat", "live_task.bat", "findpy.bat"]
MAC = ["mac_install.sh", "run.command", "live.command", "stop_live.command", "開啟儀表板.command", "移除排程.command",
       "mac/common.sh", "mac/run_task.sh", "mac/live_task.sh", "使用說明（Mac）.txt"]
EXEC = (".sh", ".command")


def read(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        sys.exit(f"找不到 {rel}")
    with open(p, "rb") as f:
        return f.read()


def eol(data, rel):
    """.bat 一定要 CRLF（Windows cmd），.sh／.command 一定要 LF（Mac 的 sh），其他文字檔維持原樣。"""
    if rel.endswith(".bat"):
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if rel.endswith(EXEC):
        return data.replace(b"\r\n", b"\n")
    return data


def add(z, top, rel, data, when):
    zi = zipfile.ZipInfo(f"{top}/{rel}", date_time=when)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.create_system = 3                                   # Unix：讓 Mac 解壓縮後保留執行權限
    mode = 0o755 if rel.endswith(EXEC) else 0o644
    zi.external_attr = (0o100000 | mode) << 16
    z.writestr(zi, data)


def build(kind, version, when):
    top = "twflow" if kind == "windows" else "twflow-mac"
    out = os.path.join(DIST, f"twflow-{kind}-{version}.zip")
    files = SHARED + (WINDOWS if kind == "windows" else MAC)
    with zipfile.ZipFile(out, "w") as z:
        for rel in files:
            add(z, top, rel, eol(read(rel), rel), when)
        for src, dst in DEFAULTS.items():
            add(z, top, dst, read(src), when)
        add(z, top, "VERSION.txt", f"{version}\n".encode(), when)
    n = len(files) + len(DEFAULTS) + 1
    print(f"  {os.path.relpath(out, ROOT)}：{n} 個檔案，{os.path.getsize(out) / 1e3:.0f} KB")
    return out


NOTES = """## 下載

| 電腦 | 檔案 |
|---|---|
| Windows | `twflow-windows-{v}.zip` |
| Mac | `twflow-mac-{v}.zip` |

### Windows
- **第一次安裝**：解壓縮到你要放的位置（例如 `D:\\twflow`），雙擊 `run.bat` 抓資料，再雙擊 `setup_task.bat` 建立自動排程。需要 Python 3.7 以上（安裝時勾「Add python.exe to PATH」）。
- **更新**：先關掉盤中服務（雙擊 `stop_live.bat` 或關掉黑色視窗），把壓縮檔裡的檔案全部複製到原本的 twflow 資料夾、選「取代」。你的 `config.json`、`sectors.csv`、`data` 資料夾不會被覆蓋。

### Mac
- 先裝 [python.org](https://www.python.org/downloads/macos/) 的 Python 3。
- 解壓縮後，打開「終端機」，輸入 `sh `（後面有一個空格），把資料夾裡的 `mac_install.sh` 拖進去，按 Enter。會安裝到 `~/twflow`。
- **更新**也是同樣步驟（`config.json`、`sectors.csv`、`data` 不會被覆蓋）。

詳細說明：壓縮檔裡的 `使用說明.txt`（Mac 另有 `使用說明（Mac）.txt`），或本儲存庫的 README。
"""


def main():
    version = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VERSION", "")).strip()
    if not re.match(r"^v?[0-9A-Za-z][0-9A-Za-z._-]*$", version):
        sys.exit("用法：python release/build_release.py v2026.10.29")
    if not version.startswith("v"):
        version = "v" + version
    os.makedirs(DIST, exist_ok=True)
    when = time.localtime()[:6]
    print(f"打包 {version}")
    build("windows", version, when)
    build("mac", version, when)
    with open(os.path.join(DIST, "RELEASE_NOTES.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(NOTES.format(v=version))
    print("  dist/RELEASE_NOTES.md")


if __name__ == "__main__":
    main()
