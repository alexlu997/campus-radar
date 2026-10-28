# 第一阶段网络定位记录

当前更新：请求级直连已接入正式采集器，并完成三站代理/TUN 开启、关闭及同进程开→关→开实测，详见本页末节与 [验收记录](ACCEPTANCE.md)。以下 06:19–06:31 内容保留为修复前的定位快照，其中“尚未接入”及“后续建议”描述当时状态。

记录时间：2026-10-28 06:19–06:31 UTC。结论以本轮本机运行态和有界 HTTPS 请求为准。

**已找到可用的单次请求路径：独立 HTTPS DNS 解析真实 IPv4，随后仅为该次请求指定 `en0` 和 `--resolve`，保留原域名、SNI 与证书校验。三个候选源的列表及既定详情共 6 页均返回 HTTP 200。** 现有全局代理路径对这些交大域名仍在 TLS 阶段超时；正式采集器尚未接入本次请求方法，这 6 页不计为原有 Fetcher 自动采集成功。

## 本机运行态

- 应用：Clash Verge 2.5.8；core：Mihomo v1.19.32。`127.0.0.1:7897` 监听属于 `verge-mihomo` PID 1297；父服务 PID 521，应用 PID 1286。
- 系统 HTTP、HTTPS、SOCKS 代理均启用并指向 `127.0.0.1:7897`。
- 从运行进程对应的本机 Unix controller 只读取得 `/configs`：`mode=global`，mixed port 为 7897，TUN 开启，设备 `utun1024`，stack 为 `gVisor`，auto-route 开启。运行中的单独 HTTP/SOCKS 端口均为 0；不能把静态配置里的 7899/7898 当作当前监听。
- DNS 使用 fake-IP；目标与控制域的系统解析均为 `198.18.0.xx`。新闻站 fake-IP 和独立解析所得的真实 IP 的普通路由均指向 `utun1024`；系统默认路由接口为 `en0`。
- 当前 GLOBAL 选择器最终指向一个 Vless 路径。API 返回 `alive=true`，最近历史延迟为 248 ms；这是其健康记录，不证明该路径可以访问每个目标。

配置文件中存在域名后缀与地理规则，但**当前全局模式不能用这些静态规则证明本次请求已经分流**。controller 快照、代理状态及路由字段见 [controller-whitelist.json](../outputs/network-diagnosis/controller-whitelist.json)、[config-whitelist.json](../outputs/network-diagnosis/config-whitelist.json) 和 [network-state.json](../outputs/network-diagnosis/network-state.json)。所有节点名称、订阅内容、secret 与账号数据均未输出。

## 失败路径与控制组

每次 curl 请求连接上限为 5 秒，总上限为 8 秒；保持 TLS 验证，不发送账号 Cookie。表中是修正指标记录格式后的同批结果。

| 目标 | 请求路径 | HTTP / TLS 结果 | 总耗时 |
| --- | --- | --- | ---: |
| news.sjtu.edu.cn | 显式 HTTP 代理 7897 | CONNECT 200；TLS 未完成；curl 28 | 5.007 s |
| me.sjtu.edu.cn | 显式 HTTP 代理 7897 | CONNECT 200；TLS 未完成；curl 28 | 5.007 s |
| ctld.sjtu.edu.cn | 显式 HTTP 代理 7897 | CONNECT 200；TLS 未完成；curl 28 | 5.003 s |
| www.sjtu.edu.cn | 显式 HTTP 代理 7897 | CONNECT 200；TLS 未完成；curl 28 | 5.005 s |
| github.com | 显式 HTTP 代理 7897 | HTTP 200；TLS 验证成功 | 1.811 s |
| example.com | 显式 HTTP 代理 7897 | HTTP 200；TLS 验证成功 | 0.935 s |
| news.sjtu.edu.cn | `--noproxy '*'`，仍走 TUN | TLS 未完成；curl 28 | 5.005 s |
| example.com | `--noproxy '*'`，仍走 TUN | HTTP 200；TLS 验证成功 | 0.988 s |

以 curl 的本地源端口关联 Mihomo `/connections`，这 8 次请求全部具有相同的匿名链编号 `path-1`。显式代理请求的连接类型为 `HTTPS`，不指定应用代理的请求类型为 `Tun`。因此，`--noproxy` 本身没有绕过当前 TUN。`path-1` 只表示本次捕获内配置链相同，不是测量得到的公网出口 IP。

四个交大域名的连接各上传约 324–326 字节、下载为 0；GitHub 与 example.com 在相同链上有正常返回。CONNECT 200 仅说明本机代理接受 CONNECT，不能单凭它声称目标服务器握手成功。超时记录中的 `ssl_verify_result=1` 出现在握手未完成时；实际退出码是 28，不应误报成已证实的证书错误。

证据见 [bounded-requests.json](../outputs/network-diagnosis/bounded-requests.json) 和按源端口关联的 [chain-summary.json](../outputs/network-diagnosis/chain-summary.json)。首轮记录器把 curl 的 `000` 状态写成了非法 JSON 数字，导致失败项指标解析错误；该轮另存为 `bounded-requests-initial-instrumentation-error.json`，不使用其缺失指标作结论。

## 请求级对照与真实输入

通过当前可用的显式代理向 Google 公共 HTTPS DNS 分别查询三个公开域名，取得下表 IPv4。查询本身保持 HTTPS 证书验证，不改变系统 DNS；TTL 为约 282–300 秒，以下 IP 是本次取样结果，不能当作永久配置。

随后指定真实 IP、`--interface en0`、`--noproxy '*'` 访问原 HTTPS URL。三个列表和三个详情均为 HTTP 200，`ssl_verify_result=0`：

| 公开输入 | 本次 IPv4 | TLS 建连时间 | 总耗时 | 字节数 |
| --- | --- | ---: | ---: | ---: |
| news 列表 `/xsjz/index.html` | 202.120.2.114 | 0.021858 s | 0.039055 s | 25,257 |
| news 详情 `/xsjz/20261023/226851.html` | 202.120.2.114 | 0.023517 s | 0.034794 s | 11,493 |
| me 列表 `/tsfx-xsbg.html` | 202.120.35.140 | 0.019539 s | 0.098473 s | 87,847 |
| me 详情 `/tsfx-xsbg/78072.html` | 202.120.35.140 | 0.024276 s | 0.095742 s | 79,160 |
| ctld 列表 `/events` | 202.120.35.142 | 0.021080 s | 1.152053 s | 98,377 |
| ctld 详情 `/events/detail/13wft64voa11do0tp1f0utg19mko91ul` | 202.120.35.142 | 0.024794 s | 0.915978 s | 77,369 |

新闻列表的请求记录显示远端为真实 IPv4，本地地址属于 `en0`；采样未观察到同一源端口的 Mihomo 连接。该次请求约 39 ms 完成，连接采样间隔为 200 ms，所以“未观察到连接”只作为补充证据，不独立证明路径。绑定接口、真实远端及 TLS/HTTP 成功共同构成请求级可用性的证据。

额外只做了一次最小区分：保留显式代理，把新闻站 CONNECT 目的地址通过 `--connect-to` 固定到同一真实 IP，同时保持新闻域名 TLS 验证。结果仍为 CONNECT 200 后约 5.006 秒 TLS 超时。因此，**仅替换代理侧的域名解析不足以修复当前代理路径**。现有证据没有区分代理上游路由、远端网络或目标对来源路径的限制，不能进一步断言是哪方屏蔽。

6 页的 URL、原始 HTML 路径与逐页 HTTP/TLS/字节指标统一登记在 [public-html-manifest.json](../outputs/network-diagnosis/public-html-manifest.json)。原始输入均由这次受限 curl 直接取得；已提供给同 HTML 解析对照。它们没有写入正式采集数据库，也不冒充原 Fetcher 或 Crawl4AI 的自动获取结果。

## 可见浏览器对照

以下由主任务在同轮实际操作观察，并转交本记录：

- 可见 IAB 打开新闻列表，导航约 20 秒超时；工具返回失败页 `news.sjtu.edu.cn took too long to respond` / `ERR_TIMED_OUT`。随后内部 data 错误页被工具 URL 策略阻止进一步读取，未尝试绕过。
- 可见 IAB 新标签打开 `https://example.com/`，约 1.85 秒成功，取得 `Example Domain` 正文。
- 日常 Chrome 对照没有完成：创建 Chrome 标签超时；原生 Chrome 读取又返回 ScreenCaptureKit `Code -3811`。因此不能声称已验证日常 Chrome 页面表现。

浏览器结果与请求矩阵的目标差异一致，但不是三站全部浏览器兼容性验收。

## 后续最小选项与当前边界

**建议第一阶段先选请求级的、仅覆盖这三个已核对来源的显式传输选项。** 使用当前物理接口及按 TTL 刷新的独立解析结果，保留原 HTTPS 域名/SNI、严格证书验证、请求上限、来源白名单与原采集证据字段。该方式不影响其他应用的全局代理；缺点是依赖本机接口/网络环境，并增加独立 DNS 查询。必须显式配置和记录选用了该路径；接口不可用、解析过期或获取失败时应报错，不能悄悄放宽 TLS 或沿未知路径继续。这里是基于已验证请求的实现建议，采集器改造由主任务统筹，尚未实施。

如果要改为系统级分流，候选是把 Clash 从 global 改为 rule，再为这三个精确域名添加 DIRECT 规则；但这会让其他流量同时恢复已有规则的行为，影响范围超过本次采集。操作前应记录原模式、原规则位置和原全局选择，形成可恢复变更，再取得对应授权；本轮没有执行。仅在 global 模式里增加域名规则不能视为有效修复。

本轮结束时仍为 `mode=global`、TUN 开启、mixed port 7897、原有系统代理与路由。未切换 VPN/代理模式，未修改系统路由或 DNS，未结束代理进程，未降低 TLS，也未触碰 DSH。已停止外部请求扩展。


## 请求级实现与实际切换验收

2026-10-28 后续落实：`campus_radar/network.py` 提供 macOS 物理接口直连，三个已验证公开来源通过 `network_mode=direct` 启用。每次请求重新读物理接口；阿里公共 HTTPS DNS 按 TTL 缓存公网 IPv4，校园 IP 不硬编码。curl 禁用隐式配置与应用代理，只绑定本次接口及解析结果；原域名、SNI、TLS 校验、主机白名单、robots、请求大小及超时约束保留。连接异常后清缓存并最多重试一次，TLS 校验错误不降级。运行范围目前为 macOS 的 `en*` IPv4 接口。

正式采集及分页已通过：三站首两页均取得，正式库 15 篇文档，十篇固定样本连续第二遍无新增或更新。详见 `outputs/phase1-qualification/live-final/report.json`。这些是正式 Fetcher 和解析器的输出，与本页前述诊断下载分开计数。

用户手动操作系统代理和 TUN，采集器没有修改设置。第一轮 3 分钟测试 27 轮均成功，但恢复晚于窗口结束，只确认开→关。第二轮同一进程持续 07:34:40–07:39:21 UTC，43 轮全部成功，覆盖三源稳定开启、关闭、恢复开启，零传输失败后自动停止。关闭期间本地脚本持续工作，不需要 GPT 在线。结果位于 `outputs/network-switch/20261028T073440-672716Z/summary.json`，逐请求状态与 HTTP/TLS 指标在同目录 `runs.jsonl`。

状态转换发生在观察到的请求之间，本次未强制打断一个在途 TCP 连接；真实开关可用性与离线故障注入的重试证据分别报告。当前其他应用继续使用用户原来的网络设置。此结果不保证任意校园网、防火墙、IPv6-only 网络或其他操作系统同样可用。
