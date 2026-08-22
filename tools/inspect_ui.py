"""UI 树自检工具。

微信控件名称随版本变化，若采集失败，运行本工具导出当前活动窗口（或指定窗口）
的 UI Automation 控件树，据此调整 config.json 里的 selectors。

用法：
    # 先在微信里手动打开搜一搜结果窗口，再运行：
    python -m tools.inspect_ui

    # 指定最大深度（默认 8）
    python -m tools.inspect_ui --depth 10

    # 只导出主窗口
    python -m tools.inspect_ui --main
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import uiautomation as auto
except Exception as exc:  # pragma: no cover
    print(f"[错误] uiautomation 不可用（仅支持 Windows）：{exc}")
    print("请执行：pip install uiautomation")
    sys.exit(1)


def dump(control, depth: int, max_depth: int, out) -> None:
    """递归打印控件树。"""
    if depth > max_depth:
        return
    indent = "  " * depth
    try:
        rect = control.BoundingRectangle
        rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
    except Exception:
        rect_str = "()"
    name = (control.Name or "").replace("\n", " ")[:60]
    line = f"{indent}{control.ControlTypeName}  Name='{name}'  Class='{control.ClassName}'  {rect_str}"
    print(line)
    out.write(line + "\n")

    try:
        for child in control.GetChildren():
            dump(child, depth + 1, max_depth, out)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="导出微信窗口 UI 控件树")
    parser.add_argument("--depth", type=int, default=8, help="最大遍历深度")
    parser.add_argument("--main", action="store_true", help="只导出微信主窗口")
    parser.add_argument("--out", default="ui_tree.txt", help="导出文件路径")
    parser.add_argument("--wait", type=float, default=3.0, help="开始前等待秒数（切换到目标窗口）")
    args = parser.parse_args()

    print(f"{args.wait} 秒后开始导出，请切换/保持目标窗口在前台……")
    time.sleep(args.wait)

    with open(args.out, "w", encoding="utf-8") as out:
        if args.main:
            for cls in ("WeChatMainWndForPC", "mmui::MainWindow", "Weixin"):
                win = auto.WindowControl(searchDepth=1, ClassName=cls)
                if win.Exists(1):
                    print(f"导出主窗口 ClassName={cls}")
                    dump(win, 0, args.depth, out)
                    break
            else:
                print("未找到微信主窗口。")
                return 1
        else:
            # 导出当前前台窗口
            fg = auto.GetForegroundControl()
            if fg is None:
                print("未获取到前台窗口。")
                return 1
            # 上溯到顶层窗口
            top = fg
            while top and top.GetParentControl() and top.GetParentControl().ControlTypeName != "PaneControl":
                parent = top.GetParentControl()
                if parent is None or parent.ClassName == "":
                    break
                top = parent
            print(f"导出前台窗口：{fg.Name} / Class={fg.ClassName}")
            dump(auto.GetRootControl(), 0, 1, out)  # 先列出所有顶层窗口
            out.write("\n--- 前台窗口详细树 ---\n")
            print("\n--- 前台窗口详细树 ---")
            dump(fg, 0, args.depth, out)

    print(f"已导出到 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
