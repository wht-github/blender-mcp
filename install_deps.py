"""
install_deps.py — 在 Blender 内置 Python 中安装依赖

在 Blender 的 Scripting 面板中运行此脚本，安装 openai 库。
"""

import subprocess
import sys

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

install("openai>=1.0.0")
print("依赖安装完成！请重启 Blender。")
