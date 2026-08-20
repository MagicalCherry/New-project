# -*- coding: utf-8 -*-
"""
更新规则.exe 的入口脚本（供 PyInstaller 打包）。

用法（打包后）：
  双击 exe            = 立即跑一轮完整更新（跳过日期自检，主动触发）
  命令行 exe run      = 按日期自检（没到周期就跳过），供定时任务/计划程序调用
  命令行 exe run --force = 同双击，立即跑
"""

import sys

import update_rules


def main():
    args = sys.argv[1:]
    interactive = not args   # 无参数 = 双击启动 = 手动更新，结束后停留等回车
    if not args:
        args = ["run", "--force"]
    sys.argv = [sys.argv[0]] + args
    try:
        update_rules.main()
    finally:
        if interactive:
            try:
                input("\n运行完毕，按回车键退出...")
            except EOFError:
                pass


if __name__ == "__main__":
    main()
