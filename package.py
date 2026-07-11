#!/usr/bin/env python3
"""
打包脚本：将 blender_agent/ 目录打包为可用于 Blender 安装的 .zip 文件

用法:
    python package.py              # 打包为 blender_agent.zip
    python package.py --output xxx # 指定输出文件名
"""

import zipfile
import argparse
from pathlib import Path

from bundle_dependencies import bundle_runtime_dependencies


def create_addon_zip(
    output_path: str = "blender_agent.zip",
    *,
    bundle_dependencies: bool = True,
    python_version: str = "3.13",
    python_platform: str | None = None,
):
    """将 blender_agent/ 目录打包为 zip 文件"""
    
    addon_dir = Path("blender_agent")
    if not addon_dir.exists():
        raise FileNotFoundError(f"找不到目录: {addon_dir}")

    if bundle_dependencies:
        bundle_runtime_dependencies(
            python_version=python_version,
            python_platform=python_platform,
        )

    libs_dir = addon_dir / "libs"
    if not libs_dir.exists() or not any(libs_dir.iterdir()):
        raise RuntimeError(
            "blender_agent/libs is missing. Run without --skip-dependencies "
            "to bundle the MCP runtime."
        )
    
    # 如果输出文件已存在，先删除
    output = Path(output_path)
    if output.exists():
        output.unlink()
        print(f"已删除旧文件: {output}")

    # 创建 zip 文件
    dependency_file_count = 0
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in addon_dir.rglob("*"):
            if file_path.is_file():
                # 跳过 __pycache__ 和 .pyc 文件
                if "__pycache__" in str(file_path) or file_path.suffix == ".pyc":
                    continue
                
                arcname = str(file_path.relative_to(addon_dir.parent))
                zf.write(file_path, arcname)
                if "libs" in file_path.relative_to(addon_dir).parts:
                    dependency_file_count += 1
                else:
                    print(f"  添加: {arcname}")
    
    file_size = output.stat().st_size / 1024  # KB
    print(f"  运行时依赖: {dependency_file_count} 个文件")
    print(f"\n打包完成: {output} ({file_size:.1f} KB)")
    print(f"安装方式: Blender → Edit → Preferences → Add-ons → Install → 选择 {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包 Blender Agent 插件")
    parser.add_argument(
        "--output", "-o",
        default="blender_agent.zip",
        help="输出文件名 (默认: blender_agent.zip)"
    )
    parser.add_argument(
        "--python-version",
        default="3.13",
        help="Blender 内置 Python 版本（默认 3.13，对应 Blender 5.x）",
    )
    parser.add_argument(
        "--python-platform",
        default=None,
        help="uv 目标平台；默认使用当前构建机平台",
    )
    parser.add_argument(
        "--skip-dependencies",
        action="store_true",
        help="复用现有 blender_agent/libs，不重新解析和打包依赖",
    )
    args = parser.parse_args()

    create_addon_zip(
        args.output,
        bundle_dependencies=not args.skip_dependencies,
        python_version=args.python_version,
        python_platform=args.python_platform,
    )
