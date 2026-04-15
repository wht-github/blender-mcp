#!/usr/bin/env python3
"""
打包脚本：将 blender_agent/ 目录打包为可用于 Blender 安装的 .zip 文件

用法:
    python package.py              # 打包为 blender_agent.zip
    python package.py --output xxx # 指定输出文件名
"""

import os
import zipfile
import argparse
from pathlib import Path


def create_addon_zip(output_path: str = "blender_agent.zip"):
    """将 blender_agent/ 目录打包为 zip 文件"""
    
    addon_dir = Path("blender_agent")
    if not addon_dir.exists():
        raise FileNotFoundError(f"找不到目录: {addon_dir}")
    
    # 如果输出文件已存在，先删除
    output = Path(output_path)
    if output.exists():
        output.unlink()
        print(f"已删除旧文件: {output}")

    # 创建 zip 文件
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in addon_dir.rglob("*"):
            if file_path.is_file():
                # 跳过 __pycache__ 和 .pyc 文件
                if "__pycache__" in str(file_path) or file_path.suffix == ".pyc":
                    continue
                
                arcname = str(file_path.relative_to(addon_dir.parent))
                zf.write(file_path, arcname)
                print(f"  添加: {arcname}")
    
    file_size = output.stat().st_size / 1024  # KB
    print(f"\n打包完成: {output} ({file_size:.1f} KB)")
    print(f"安装方式: Blender → Edit → Preferences → Add-ons → Install → 选择 {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包 Blender Agent 插件")
    parser.add_argument(
        "--output", "-o",
        default="blender_agent.zip",
        help="输出文件名 (默认: blender_agent.zip)"
    )
    args = parser.parse_args()
    
    create_addon_zip(args.output)
