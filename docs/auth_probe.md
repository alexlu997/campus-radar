# 第二课堂本机登录验证路径

状态：**验证工具已准备；未启动真实登录，未读取账号内容，未做登录后实测。** 本阶段只准备路径。公开信息源采集不依赖本工具。

## 范围与依赖

- 两个候选入口分别处理：`dekt` → <https://dekt.sjtu.edu.cn/h5/>；`activity` → <https://activity.sjtu.edu.cn/>。尚未证明它们同源、接口相同或网络条件相同。
- 工具使用 Python 3 和 Playwright；本机 Python 3.13 已发现 Playwright 安装。浏览器使用本机 Google Chrome 的独立配置，未验证当前机器上的实际启动结果。
- 当前版本只观察用户选定页面的 DOM 结构计数，不提取正文、活动字段、链接地址、接口响应、截图或网络日志。没有已观察到的活动 DOM/API，不能猜测字段或路由。
- 不支持自动报名、申请、取消、下载名单或探查其他校园业务。

## 日后获得读取授权后，在独立本地终端运行

先进入项目目录。以下命令是本工具实际提供的接口，登录命令本轮没有执行：

```sh
cd /absolute/path/campus-radar
python3 -m campus_radar.auth --help
python3 -m campus_radar.auth login --source dekt
```

工具会打开独立 Chrome 窗口。用户自行核对真实交大认证页面并输入密码、扫码或完成验证码；程序不监听认证请求，也不记录按键。完成后在终端输入 `DONE`，窗口关闭并保留本项目会话。`login_user_confirmed_not_verified` 只表示用户完成确认，不是认证有效或活动接口已接通的结论。

然后按需要验证一个活动列表或详情页面：

```sh
python3 -m campus_radar.auth probe --source dekt --page-kind list
python3 -m campus_radar.auth probe --source dekt --page-kind detail
```

每次都从候选入口打开新的标签页。用户在**刚打开的同一标签页**手动进入自己有权查看的活动列表或详情，不点击报名、取消或申请。确认页面正确后，在终端输入 `READ`。程序仅检查该标签页，不自动点击、翻页、遍历链接或请求业务 API。`--page-kind` 是用户选择的页面类型，不是程序识别结果。

验证另一个候选入口时，将 `--source dekt` 换为 `--source activity`。两个来源使用不同浏览器配置，不自动共享登录态。

`login` 和 `probe` 拒绝非交互式标准输入，避免被后台批量运行。退出码 0 表示本次有限操作完成；`structure_observed` 也只说明取得结构计数，**不代表已登录、页面确实含活动或字段已经解析**。

## 报告边界与状态

终端 JSON 白名单仅包含：来源代号、UTC 检查时间、固定状态码、用户声明的列表／详情类型、观察页数，以及链接、图片、列表项、表格行、iframe、密码输入框的数量。计数不是活动数量，不可计算发现率或覆盖率。

报告不含当前网址、查询参数、片段、页面标题、正文、用户姓名、Cookie、Token 或任何请求头。页面跳转到已知 jAccount 认证域时报告 `authentication_required`；离开所选来源域时报告 `unexpected_page`，并停止 DOM 检查。来源域内的空壳页也可能有结构计数，因此活动内容与登录状态仍为未验证。

| 状态 | 含义 |
| --- | --- |
| `login_user_confirmed_not_verified` | 用户声明认证完成；工具没有验证账号有效性 |
| `structure_observed` | 只读取了一个所选来源页面的结构计数 |
| `authentication_required` | 当前是已知认证页面，或有密码输入框 |
| `unexpected_page` | 当前页面不在所选来源的 HTTPS 域名范围内 |
| `local_interactive_terminal_required` | 没有本地交互式终端，未启动浏览器 |
| `playwright_not_installed` | 当前 Python 无法导入 Playwright |
| `failed_no_sensitive_diagnostics` | 浏览器、网络或受保护目录操作失败；没有打印可能带凭据的原始异常 |
| `cancelled` | 用户取消本次操作 |

失败后先由用户在本地检查 Chrome 是否可用、网络和页面是否正常。不要把完整网络日志、错误堆栈、认证网址或浏览器配置发给 Codex。工具没有确认的字段均输出 `not_verified`，接口状态为 `not_observed`。

## 存储与清理

独立配置位于仓库之外：

```text
~/Library/Application Support/Campus Radar/auth/dekt/
~/Library/Application Support/Campus Radar/auth/activity/
```

配置可能含可冒用账号的会话数据，目录权限为 `0700`，本项目标记文件权限为 `0600`。不得打开或复制配置文件到模型上下文、Git、示例或测试夹具。本工具不导出认证状态，也不保存报告文件；需要保留结论时只使用上述脱敏 JSON。

关闭本项目的浏览器窗口后可清除对应来源的认证状态：

```sh
python3 -m campus_radar.auth clear --source dekt
python3 -m campus_radar.auth clear --source activity
```

清理仅限带有匹配项目标记的固定来源目录；路径为符号链接、缺少标记、标记不匹配或存在浏览器锁时拒绝操作。不会清除日常 Chrome 配置或其他来源。卸载插件本身不等同于清除认证状态。

## 离线验证与未验证项

离线检查命令：

```sh
python3 -m unittest discover -s tests -p test_auth.py -v
```

测试只使用临时目录和虚拟页面对象，覆盖私有权限、路径与标记保护、浏览器锁、认证页／外域拒绝、报告白名单、异常脱敏及非交互式运行限制。所有虚拟值均为测试输入，不是校园账号或真实接口结果。

本轮已在本机 Python 3.13 运行上述命令：**11 项离线测试通过**；`python3 -m campus_radar.auth --help` 也已执行。没有执行 `login`、`probe` 或针对真实认证目录的 `clear`。

仍待用户日后参与验证：实际浏览器启动和网络可达性、未登录／登录后差异、会话复用和失效、列表与分页方式、活动字段及原文证据、只读接口的真实业务语义、账号可见范围、两个入口的关系，以及授权自动读取的限制。当前工具不证明这些事项已经通过。
