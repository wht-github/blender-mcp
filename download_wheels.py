#!/usr/bin/env python3
"""
download_wheels.py — 下载 openai 及其依赖并解压到 blender_agent/libs/

用法:
    python download_wheels.py                          # 默认: Blender 5.1 (cp313, win_amd64)
    python download_wheels.py --python-version 3.13    # 指定 Python 版本
    python download_wheels.py --platform linux_x86_64  # 指定平台

Blender 内置 Python 版本参考:
    Blender 4.0      → Python 3.10  (cp310)
    Blender 4.1~4.3  → Python 3.11  (cp311)
    Blender 4.4~4.5  → Python 3.12  (cp312)
    Blender 5.0+     → Python 3.13  (cp313)

下载完成后，依赖会解压到 blender_agent/libs/ 目录下，
随 addon 一起分发，无需用户手动 pip install。
注意：wheel 中包含 .pyd/.so 等原生扩展，不能从 zip 内加载，因此必须解压。
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

LIBS_DIR = Path("blender_agent") / "libs"

# 需要下载的包（openai 及其所有运行时依赖会被自动解析）
PACKAGES = ["openai>=1.0.0"]


def download_wheels(python_version: str, platform: str):
    # 清空旧的 libs 目录
    if LIBS_DIR.exists():
        shutil.rmtree(LIBS_DIR)
    LIBS_DIR.mkdir(parents=True)

    print(f"目标: Python {python_version}, 平台 {platform}")

    # 使用 pip download 下载 wheel 文件到临时目录
    # 若当前环境没有 pip，先通过 uv 安装
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print("pip 不可用，尝试通过 uv 安装 pip ...")
        subprocess.check_call(["uv", "pip", "install", "pip"])

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"正在下载 wheels ...")

        cmd = [
            sys.executable, "-m", "pip", "download",
            *PACKAGES,
            "--dest", tmp_dir,
            "--only-binary=:all:",
            "--python-version", python_version,
            "--platform", platform,
        ]
        subprocess.check_call(cmd)

        # 解压所有 wheel 到 libs/
        wheels = sorted(Path(tmp_dir).glob("*.whl"))
        print(f"\n共下载 {len(wheels)} 个 wheel，正在解压到 {LIBS_DIR} ...")
        for whl in wheels:
            print(f"  解压: {whl.name}")
            with zipfile.ZipFile(whl, "r") as zf:
                zf.extractall(LIBS_DIR)

    # 删除 *.dist-info 目录（不需要）
    for dist_info in LIBS_DIR.glob("*.dist-info"):
        shutil.rmtree(dist_info)

    # 统计
    total_files = sum(1 for _ in LIBS_DIR.rglob("*") if _.is_file())
    total_size_mb = sum(f.stat().st_size for f in LIBS_DIR.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"\n解压完成: {total_files} 个文件, {total_size_mb:.1f} MB")
    print(f"依赖已就绪，打包时会自动包含在 addon zip 中。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="下载 openai wheel 文件")
    parser.add_argument(
        "--python-version", default="3.13",
        help="目标 Python 版本 (默认: 3.13，对应 Blender 5.0+)",
    )
    parser.add_argument(
        "--platform", default="win_amd64",
        help="目标平台 (默认: win_amd64; 可选: linux_x86_64, macosx_11_0_arm64 等)",
    )
    args = parser.parse_args()

    download_wheels(args.python_version, args.platform)
