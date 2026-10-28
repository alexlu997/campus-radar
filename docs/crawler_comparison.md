# Crawl4AI 小样本对照

核查日期：2026-10-28。结论：本次没有证据支持直接替换现有采集器。Crawl4AI 能从已取得的真实 HTML 生成通用正文和 Markdown，但不能自动解决网络路径、栏目筛选、分页、活动字段和多截止时间的业务规则。

## 环境与范围

- 独立环境：`outputs/crawler-comparison/venv`；Python 3.13.2，Crawl4AI **0.9.5**，Playwright **1.64.1**，lxml **6.1.3**，aiohttp **3.14.5**。
- 复用 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，版本 **156.0.8078.74**；没有执行浏览器下载或 `crawl4ai-setup`。
- 共 96 个已安装 distribution（含基础安装工具），环境占用 728388 KiB，约 711.3 MiB。依赖已锁在 `outputs/crawler-comparison/requirements.lock.txt`；`pip check` 通过。全局 Python 未安装新包。
- 没有配置或调用模型提取、Crawl4AI Cloud、付费 API；禁用 `.env` 自动加载和 LiteLLM 远程价格表加载。没有登录、读取日常浏览器配置、修改系统代理或写入 `public.sqlite3`。
- 两组使用同样 6 个来源 URL：news、ME、CTLD 各一个列表和来源主清单已有详情。输入见 `outputs/crawler-comparison/inputs.json`。

官方依据：[PyPI 0.9.5](https://pypi.org/project/Crawl4AI/0.9.5/)、[安装说明](https://docs.crawl4ai.com/core/installation/)、[配置参数](https://docs.crawl4ai.com/api/parameters/)。实际参数和 robots 行为还核对了本次安装的源码。

## 默认网络获取对照

每请求超时 15 秒、重试 0、缓存关闭、相同 User-Agent；Crawl4AI 使用现有 Chrome，`ignore_https_errors=False`、`check_robots_txt=True`、`enable_stealth=False`，未启用用户模拟或验证码处理。

| 方法 | 目标输入 | 成功取得 HTML | 失败位置 | 每条实测耗时 |
| --- | ---: | ---: | --- | --- |
| 现有 Fetcher | 6 | 0 | robots.txt 请求超时，按既有规则停止 | 15.021–15.026 秒 |
| Crawl4AI 默认浏览器路径 | 6 | 0 | robots 检查返回允许后，页面导航超时 | 17.043–17.164 秒 |

首次浏览器启动实测 1.628 秒。没有成功页面，因此这些数字只表示本次失败路径成本，不能排名吞吐或有效采集速度。

**robots 口径并不等价。** 当前 Fetcher 在 robots 不可验证时停止，404 才视为无规则。Crawl4AI 0.9.5 的 `utils.RobotsParser.can_fetch` 使用 2 秒 aiohttp 请求，对任何异常或非 200 返回允许；未设置 `trust_env`，其默认值为 false。代理取自 `egress_policy.proxy_url()`，本次为默认 None，不自动采用 HTTP_PROXY/HTTPS_PROXY。实测 6 次均约 2 秒后返回允许、没有缓存规则；库没有暴露具体 HTTP／异常原因。故即使后者取得页面，也不能把政策差异直接算作框架优势。本次两者均未取得页面。

证据：`outputs/crawler-comparison/network-default-1/results.json` 及同名 `.log`。

## 同一份真实 HTML 的处理对照

输入来自 `outputs/network-diagnosis/public-html-manifest.json`：诊断分支以单次受限 curl、真实 IPv4、en0 和原 HTTPS 主机名取得，6 份均 HTTP 200、TLS 校验 0。此组不请求学校页面；Crawl4AI 使用 `raw:` 输入，**不代表其浏览器直接获取成功**。原始输入路径与 SHA-256 保留在结果中。

| 输入 | 既有解析结果 | Crawl4AI 通用文本结果 |
| --- | --- | --- |
| news 列表 | 10 条链接；未发现下一页 | 生成 1406 字符 Markdown；共用解析器仍为 10 条 |
| ME 列表 | 12 条链接；未发现下一页 | 生成 13908 字符 Markdown；共用解析器仍为 12 条 |
| CTLD 列表 | 13 条链接；未发现下一页 | 生成 8487 字符 Markdown；共用解析器仍为 13 条 |
| news 详情 | 正文 514 字符，但未识别活动 | 通用净化文本 742 字符，保留完整活动时间和地点；同一字段规则只提取日期 2026-10-27 和楼名，漏掉时刻与教室号 |
| ME 详情 | 正文选择器未匹配 | 通用净化文本 3253 字符；同一字段规则识别 2026-11-09 14:00–15:00、A 楼振华会议室；资格未知 |
| CTLD 征稿详情 | 正文选择器未匹配 | 通用净化文本 3321 字符，保留日期范围和参会对象；同一字段规则只提取会议起日 2027-04-16 与闵行校区，未完整处理 16–18 日范围及两类投稿截止 |

这里的“字段”使用同一套现有规则解析 Crawl4AI 净化文本，不是模型推断，也不是 Crawl4AI 原生领域识别。其通用正文仍可能包含导航和页尾；Markdown 字符数不是有效正文量。

`parser_status=parsed` 只表示程序返回了结果。人工核对发现 CTLD 的 13 条包含顶部“2026年学术年会”导航，实际活动卡为 12 条；三个列表都仍有下一页机制而基线未发现，卡片标题也混入其他文本。不能据此宣称列表准确或分页完整。三详情的完整资格均保持未知；未把未来发布时间、开始日期或缺失截止解释为可以报名。

当前证据：`outputs/crawler-comparison/raw-html-3/results.json`、各页 `.crawl4ai.html`／`.crawl4ai.md`。12 条结果的 `target_requested` 均为 false；逐条的 `network_success`、`accepted_under_common_policy` 及汇总的 `network_successes`、`common_policy_accepted` 均为 null（不适用）。真实 HTML 处理的单次耗时为基线 0.001–0.004 秒、Crawl4AI 0.006–0.024 秒；样本小且未控制预热，不作性能优劣结论。

`raw-html-1` 保留为原始运行，其中汇总曾将不适用的网络指标相加成 0；不使用该汇总判定网络成功率。`raw-html-2` 是修正后的沙箱内运行，Chrome 启动失败，不计入正文处理对照；`raw-html-3` 使用相同本地输入，在允许启动现有 Chrome 的环境完成复跑。原有 `network-default-1` 网络结果未改写，也未重发网络测试。

## 独立人工字段核对

人工核对使用诊断取得的完整 HTML，与通用正文清洗及现有字段规则的结果分别比较：

- **news：**[原始 HTML](../outputs/network-diagnosis/news-detail-bound-en0.html)明确列出 `2026-10-27 18:30–20:00` 和“闵行校区陈瑞球楼 100 号”。原解析器选择的正文容器不包含这些字段，故 `event=null`；Crawl4AI 净化正文保留了这些文本，但日期、时刻、楼名和教室号分行，现有字段规则只生成日期与楼名。该页的遗漏同时涉及正文范围与跨行字段处理。
- **ME：**[原始 HTML](../outputs/network-diagnosis/me-detail-bound-en0.html)有完整标题、`2026-11-09 14:00–15:00` 及 A 楼振华会议室，Crawl4AI 净化后的文本供现有规则提取出时间地点；既有站点解析器因标题／正文选择器不匹配而失败。页内“发布时间：2026-11-09”晚于本轮核查日，应保留为来源日期异常，不能据此确认真实发布时间；参加资格仍未明确。
- **CTLD：**[原始 HTML](../outputs/network-diagnosis/ctld-detail-bound-en0.html)明确给出 `2027-04-16–18`、上海交通大学闵行校区（线下），以及一线高校教师、高校教学管理人员、高校教学发展工作人员、高校教学研究人员四类参会对象。Crawl4AI 净化正文保留了这些内容，现有字段规则只提取起日和地点，未提取结束日和部分对象；完整准入及注册条件仍未知。页面的 `00:00–23:59` 不足以证明会议实际时刻，文末 `2026-10-12` 也只作为署名日期，不能升格为 CMS 发布时间。

`docs/samples_review.json` 中 CTLD 时间、地点的旧 `unknown` 来自此前仅获得局部正文。完整 HTML 已增加证据，不能将当前解析缺失与旧基准的 null 一致计作字段正确。本次没有改写该旧基准，也没有宣称完成 10 篇全量字段正确率验收。

## 复跑与检查

从仓库根目录运行，选择新的输出目录以保留旧证据：

```sh
outputs/crawler-comparison/venv/bin/python tools/compare_crawlers.py --output outputs/crawler-comparison/network-next --timeout 15
outputs/crawler-comparison/venv/bin/python tools/compare_crawlers.py --html-manifest outputs/network-diagnosis/public-html-manifest.json --output outputs/crawler-comparison/raw-next
outputs/crawler-comparison/venv/bin/python -m unittest discover -s tests -p test_compare_crawlers.py -v
```

7 项离线测试通过，覆盖登录页拒绝、未知字段、跨主机重定向、robots 口径、6 输入约束、TLS 证据要求与有界错误输出。测试里的合成 HTML 仅验证判定逻辑，未混入真实对照结果。生产 collector/storage、本轮来源主清单与业务数据库均未因本对照修改。

下一步应先修复已明确的站点适配和正常网络路径，再决定是否为通用正文提取引入 Crawl4AI；当前没有必要为了这 3 个站点把整个依赖栈并入生产插件。
