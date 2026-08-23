from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "wxsearch" / "templates" / "leads_dashboard.html"


def test_filter_mode_is_only_set_from_switch_tab_argument():
    """页面初始化不能引用 switchTab 作用域之外的 mode。"""
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "_currentFilterMode = mode;" in html
    assert html.count("_currentFilterMode = mode;") == 1
    assert "const activeBtn = document.getElementById(`tab-${mode}`);" in html
    assert html.count("const activeBtn = document.getElementById(`tab-${mode}`);") == 1
