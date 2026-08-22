"""分渠道采集驱动包：PC 搜一搜(UIA) / 搜狗微信(Playwright) / 手机搜一搜(待接)。

各驱动接口一致：open_search(keyword) / apply_filters(win) / iter_articles(win, keyword)，
由 Collector 按渠道无感切换。
"""
