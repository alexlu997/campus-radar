# 本机 DSH 侧栏验证

此页保留开发阶段的本地链接验收方法；预览版用户使用 [安装说明](INSTALL.md)中的完整包。下文绝对路径均为示例，需要替换为自己的路径。

本机安装为 **DeepSeek Harness 0.2.1-rc.3**，应用路径 `/Applications/DeepSeek Harness.app`。本页依据该安装内的 CLI、Desktop Host、插件管理器和 Web 组合包实现整理。测试使用真正的 Electron 桌面载体；CLI Web profile 可用于接口预检，但不能代替原生侧栏验收。

## 隔离启动

```sh
cd /absolute/path/campus-radar
./tools/start_dsh_isolated.sh
```

脚本设置两处独立目录：

- Harness 数据：`outputs/dsh-home`，原生 profile 为 `outputs/dsh-home/profiles/desktop`。
- Electron 窗口、缓存与偏好：`/private/tmp/campus-radar-dsh-electron`，通过 `--user-data-dir` 指定。

只设置 `DSH_HOME` 不会隔离 Electron 的 `userData`。本次启动已观察到两个独立目录、独立 `SingletonLock` 和 desktop profile，以及原生 Electron 的两个 renderer target。日常 DSH 的 `~/.dsh` 与默认 Electron 配置不作为本测试的输入。

验收时可用 `./tools/start_dsh_isolated.sh --debug`，调试接口仅监听 `127.0.0.1:19222`。此端口属于测试实例，测试完成后退出该实例即关闭。普通启动不开放调试端口；改变调试参数前先退出当前测试实例。脚本调用本机已安装的桌面可执行文件。退出时用此实例自己的“退出”菜单或启动终端中断；不要按进程名称批量结束其他 DSH 窗口。

本机对照发现：把 Electron userData 放在仓库的 `Documents` 路径时，进程停在文件打开操作且没有 renderer；将该目录改到 `/private/tmp` 后正常产生原生页面与 Host 服务。这里记录的是路径对照结果，未把系统文件授权列为已证实根因。临时目录可能由系统清理；插件与数据库配置保存在项目的独立 Harness home 中。

## 历史开发链接安装

首次运行隔离实例会由 DSH 初始化 desktop profile。随后退出该实例，再在项目目录执行：

```sh
DSH_HOME="$PWD/outputs/dsh-home" \
  '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' \
  plugin --profile desktop add "link:$PWD/plugin" --offline --ignore-scripts
```

这是 DSH 自带 CLI 与 pnpm；无需 npm 发布、下载依赖或执行依赖构建脚本。安装已经实际完成，profile 的依赖及 `dsh.profile.bundles` 均包含 `campus-radar-dsh`，本地符号链接指向仓库的 `plugin/`。插件包 `dsh.bundle.patch` 指向 `cordis.patch.yml`，其中插入 `campus-radar` 条目。

配置保存在隔离 profile 的 `cordis.patch.yml`：

```yaml
- id: campus-radar
  config:
    python: /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
    repoRoot: /absolute/path/campus-radar
    dbPath: /Users/your-user/Library/Application Support/CampusRadar/public.sqlite3
```

该阶段 `dbPath` 指向 Application Support 正式库，加入 ME 砺远学术后包含 40 篇文档、34 条活动或截止事项。原项目内 `outputs/public.sqlite3` 保留为转移时副本。更早的 `outputs/sidebar-verification.sqlite3` 也独立保留，其中 1 条 `manual_reference` 只属于宿主测试，没有混入正式库。第二阶段已在真实原生实例通过筛选、详情、收藏、反馈、偏好、手动更新、简报及重启持久化；新增公众号的双出处展示与限流提示也已实际核对，见 [接入记录](werss.md)。

插件代码通过本地链接读取，重新启动加载。第一阶段普通模式及停用／启用证据保留在 `outputs/phase1-qualification/`；第二阶段验收见 `docs/PHASE2.md` 和 `outputs/phase2-qualification/`。DSH 插件页面可停用该组合包或 `campus-radar` 条目，停用只改变本隔离 profile。

## 当前接口和后台运行

Host 只注册 `plugin/index.js` 的固定操作白名单，路由为精确的 `POST /api/campus-radar/<operation>`，继承 DSH 已认证连接的权限检查。固定 Python `api` 命令调用与 CLI、调度共用的 `service.dispatch`。允许的业务包括列表、详情、收藏、反馈、偏好、来源启停、简报、更新与进度、调度设置、官网搜索和候选文章；参数通过字段及类型校验。前端不能指定任意命令或数据库路径，不提供账号登录或报名接口。

每日任务由独立的 `org.campusradar.daily` launchd 任务检查，正式设置为上海时间 08:00。`schedule-set` 只保存时间／启停设置；本机触发器已由 `python3 -m campus_radar schedule-install` 安装。每日运行使用 Application Support 的源码副本，修改 Python／来源配置后需重新执行该安装命令同步。关闭 DSH 不关闭已启动的后台采集或每日任务。

两阶段均未使用校园账号或发起 DeepSeek 模型任务，也未执行 DeepSeek 登录；测试界面显示 `Signed in to DeepSeek`，匿名宿主可用性未验证。第二课堂登录准备见 `auth_probe.md`；不会因插件安装而运行登录工具。

启动日志可能包含 DSH 本机访问 token，不应整份复制到聊天、提交 Git 或当作普通验收附件。证据只记录去除凭据的版本、路径、操作结果和界面截图。`outputs/dsh-home` 是运行状态，应从版本控制排除；临时 Electron 目录也不属于交付代码。

## CLI Web 预检路径

已单独初始化 `campus-radar` Web profile。需要时可以这样启动：

```sh
DSH_HOME="$PWD/outputs/dsh-home" \
  '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' \
  --profile campus-radar --port 18765 --no-open
```

`--from-default-profile web` 会重置自定义 profile，只用于最初创建，不属于日常启动命令。Web profile 及其插件配置与原生 `desktop` profile 各自独立。

## 预览版完整包迁移记录

2026-10-28 已在明确部署授权后，从旧 `link:` 开发包迁移到 `0.2.0-preview.2` 完整包。使用原 profile 的既有 pnpm store；旧锁文件仍解析到开发链接时，先用 DSH CLI 移除该插件依赖，再安装完整包。只直接覆盖依赖地址可能保留旧链接，此次以安装后的版本、命令入口及文件内容验证结果。数据库和 profile 配置已预先备份。

当前插件只需 `dataDir`，Python 来自该目录的独立 venv，代码来自已安装包；后台运行文件仍部署到用户数据目录。40 篇文档、34 个活动、原有设置和公众号启用选择保留，真实原生界面及后台代码一致性通过，最后恢复普通启动。新用户直接按 [INSTALL.md](INSTALL.md)安装，无需复现开发链接迁移。
