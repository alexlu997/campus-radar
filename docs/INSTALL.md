# 校园雷达 macOS 预览版安装说明

当前候选版本为 `0.2.0-preview.2`。支持范围为 macOS、Python 3.13 和 DSH `0.2.1-rc.3`；其他 DSH 版本尚未验证，包清单会阻止不匹配的版本。源码采用 Apache-2.0 许可证。仓库为 [alexlu997/campus-radar](https://github.com/alexlu997/campus-radar)。本地文件与 GitHub 标签安装分别验收。

## 安装与首次初始化

先确认 `python3 --version` 为 3.13.x。插件不会修改系统 Python。下列命令使用 DSH 桌面版自带的 CLI；若已将 `dsh` 加入 PATH，也可直接使用 `dsh`。

```sh
dsh_cli="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
"$dsh_cli" plugin --profile desktop add ./campus-radar-dsh-0.2.0-preview.2.tgz --ignore-scripts
"$dsh_cli" plugin --profile desktop exec campus-radar setup
```

把安装包路径替换为实际下载位置，profile 名替换为准备使用的 profile。`setup` 在用户数据目录中创建独立 Python 环境，并安装 `requirements.txt` 中固定版本的 lxml；这一步需要联网。它不启用定时任务，也不进行微信登录。

随后重新启动该 DSH 实例，在侧栏打开“校园雷达”，点击更新来源。新安装默认启用新闻学术网、机动学院学术报告和教学发展中心三个官网栏目。无需模型或搜索 API Key。

首次打开若显示“Python 环境尚未就绪”，先完成上述 `setup`；不要把初始化失败当成没有活动。已安装环境缺少 lxml 时，也可重新运行该命令修复。

GitHub 标签安装命令如下，之后执行同一条 `setup` 命令：

```sh
"$dsh_cli" plugin --profile desktop add github:alexlu997/campus-radar#v0.2.0-preview.2 --ignore-scripts
"$dsh_cli" plugin --profile desktop exec campus-radar setup
```

固定标签或提交可明确安装版本。发布流程参考 [DSH 官方说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.zh.md)。

## 数据与可选公众号

默认位置为 `~/Library/Application Support/CampusRadar/`：

- `public.sqlite3`：公开文档、活动、收藏、偏好和简报。
- `venv/`：独立 Python 环境。
- `runtime/`：启用后台调度时部署的运行代码。
- `public-page-cache/`：网页缓存。

这些内容不随插件分发。ME 砺远学术默认关闭，需要用户自行配置本机 WeRSS／微信读书授权后在来源页启用；未配置时有明确错误，官网功能仍可使用。当前适配器只检查最新一篇，不能回补历史，限流恢复和长期稳定性尚未通过。安装和升级不会替用户扫码，也不会重置已有启用状态。

## 每日后台更新

```sh
"$dsh_cli" plugin --profile desktop exec campus-radar schedule-install
```

然后在“偏好与定时”中启用每日更新，设置时间和时区。可沿用上海时间 08:00。后台由本机登录用户的 launchd 执行，退出 DSH 后仍可工作；电脑关机或用户未登录时不执行。

每分钟检查一次是否到期；单日最多运行三批，批次间隔至少 15 分钟，失败或待补扫时可在预算内继续。预算用尽后保留待办，后续更新继续，也可手动更新。来源页分别显示本轮请求结果与剩余补扫范围。

暂停只需在页面关闭每日更新。完全移除后台触发器使用下节命令。真实整机睡眠／唤醒尚未验证。

## 升级与卸载

升级前等待当前采集结束。安装新版本后重新运行 `setup`；若此前启用了后台任务，再运行 `schedule-install` 同步后台代码。升级使用已有数据库，收藏、反馈、偏好和简报保留。后台配置必须属于同一数据库才能替换；后台安装失败会尝试恢复原配置和代码，并报告失败。

卸载流程：

```sh
"$dsh_cli" plugin --profile desktop exec campus-radar schedule-uninstall
"$dsh_cli" plugin --profile desktop remove campus-radar-dsh
```

第一步停止并移除该数据库的后台触发器；若采集尚在执行，会要求等待结束再重试。第二步移除 DSH 组合包。若从未初始化或安装后台任务，可直接移除组合包。

CLI 移除后重新启动该 DSH 实例，侧栏才会清除已加载的插件入口。再次安装后也需要重新启动。

**只在 DSH 中移除组合包不会自动运行后台卸载命令。** 已启用后台任务时请按上述顺序操作。数据库和 Python 环境默认保留，以便重新安装；不执行全目录清理。

## 独立数据目录

测试或第二个独立实例可使用不同目录：

```sh
"$dsh_cli" plugin --profile preview exec campus-radar --data-dir "/absolute/path/CampusRadarPreview" setup
```

在对应 profile 的 `cordis.patch.yml` 中为插件指定同一绝对路径：

```yaml
- id: campus-radar
  config:
    dataDir: /absolute/path/CampusRadarPreview
```

其后的命令也需要同一 `--data-dir`。不同数据库使用不同后台任务名称，不会覆盖默认实例。也可通过 `CAMPUS_RADAR_HOME` 指定数据目录。

既有 profile 中的 `python`、`repoRoot`、`dbPath` 配置仍被支持；普通新安装不需要填写它们。源码 checkout 中的 `plugin/package.json` 只保留给已有本地链接使用，发行包以仓库根目录的 `package.json` 为准。
