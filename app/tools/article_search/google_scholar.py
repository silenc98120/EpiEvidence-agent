"""Google Scholar 检索器暂缓。

Google Scholar 没有适合当前自动化工作流的稳定官方检索 API。第一版不使用
页面抓取、非官方代理或模拟浏览器查询，避免服务条款、限流和结果可复现性问题。
"""

GOOGLE_SCHOLAR_SUPPORTED = False
GOOGLE_SCHOLAR_UNAVAILABLE_REASON = "缺少稳定、可复现的官方检索 API"
