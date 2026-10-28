window.__ModuleLoader__.load({
  id: "campus-radar-dsh",
  factory: (require) => {
    const { createElement: h, useEffect, useState } = require("react");
    const labels = { failed: "采集失败", partial: "部分完成", success: "成功", running: "执行中", queued: "等待执行", interrupted: "执行中断", idle: "尚未更新" };
    const statuses = { past: "历史活动", ongoing: "进行中", started_end_unknown: "已开始／结束时间未知", scheduled_registration_unknown: "已有日程／报名状态未知", recap: "活动回顾", postponed: "已延期", unknown: "时间待核对", cancelled: "已取消", needs_review: "内容有冲突或待重新核对", deadline_open: "截止尚未到／资格待核对", deadline_passed: "已截止" };
    const sections = { new: "新发现", deadlines: "临近截止", changes: "重要变更", explore: "探索" };
    const time = (value) => !value ? "未记录" : /^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "（时刻未知）" : new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
    const link = (value) => { try { const u = new URL(value); return ["https:", "http:"].includes(u.protocol) ? u.href : undefined; } catch { return undefined; } };
    const anchor = (url, title) => h("a", { href: link(url), target: "_blank", rel: "noopener noreferrer" }, title);
    const styles = `
      .campus-radar{height:100%;overflow:auto;padding:calc(var(--dsh-frame-top-clearance, 0px) + 28px) 36px 36px;color:inherit;font:inherit;box-sizing:border-box}
      .campus-radar *{box-sizing:border-box}.campus-radar h1{font-size:26px;letter-spacing:-.5px;margin:0 0 8px}.campus-radar h2{font-size:18px;margin:28px 0 14px}
      .campus-radar p{line-height:1.7;margin:8px 0}.campus-radar small{opacity:.7}.campus-radar header,.campus-radar nav,.campus-radar form{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
      .campus-radar button,.campus-radar input,.campus-radar select{font:inherit;border:1px solid #8886;border-radius:8px;background:transparent;color:inherit;padding:8px 12px}.campus-radar button{cursor:pointer}.campus-radar button:disabled{opacity:.5}.campus-radar label{display:flex;gap:6px;align-items:center}.campus-radar select option{color:CanvasText;background:Canvas}
      .campus-radar nav{margin:20px 0}.campus-radar button[aria-pressed=true]{border-color:#4580ef}.campus-radar .notice{padding:12px 16px;background:#b7891515;border:1px solid #b7891544;border-radius:10px;margin:16px 0}
      .campus-radar .document{padding:18px 0;border-top:1px solid #8884}.campus-radar .document h3{font-size:16px;line-height:1.6;margin:4px 0 10px}
      .campus-radar a{color:inherit;text-decoration:underline;text-underline-offset:3px}.campus-radar dl{display:grid;grid-template-columns:90px 1fr;gap:7px 12px;font-size:14px}.campus-radar dt{opacity:.65}.campus-radar dd{margin:0;overflow-wrap:anywhere}
      .campus-radar .sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.campus-radar .source{border:1px solid #8884;border-radius:10px;padding:14px;font-size:13px}.campus-radar .source strong{display:block;margin-bottom:8px}
      .campus-radar .body,.campus-radar pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;max-width:900px}.campus-radar :focus-visible{outline:2px solid #4580ef;outline-offset:3px}
      @media(max-width:700px){.campus-radar{padding-left:20px;padding-right:20px}.campus-radar input{max-width:220px}}
    `;
    function Panel({ call }) {
      const [data, setData] = useState(null);
      const [loading, setLoading] = useState(true);
      const [error, setError] = useState("");
      const [generation, setGeneration] = useState(0);
      const [tab, setTab] = useState("digest");
      const [filters, setFilters] = useState({});
      const [offset, setOffset] = useState(0);
      const [job, setJob] = useState({ status: "idle" });
      const [busy, setBusy] = useState(false);
      const [notice, setNotice] = useState("");
      const [details, setDetails] = useState({});
      const [day, setDay] = useState("");
      const api = async (method, payload = {}) => {
        const result = await call(method, payload);
        if (!result.ok) throw new Error(result.error?.message || "操作未完成");
        return result.value;
      };
      useEffect(() => {
        let active = true;
        setLoading(true); setError("");
        const documentFilters = Object.fromEntries(Object.entries(filters).filter(([key]) => ["query", "source_id", "category"].includes(key)));
        Promise.all([
          api("sources"), api(tab === "documents" ? "list" : "events", { ...(tab === "documents" ? documentFilters : filters), ...(tab === "favorites" ? { favorites: true } : {}), limit: 50, offset }),
          api("digest", day ? { day } : {}), api("preferences"), api("update-status"), api("digests"), api("schedule"), api("candidates"),
        ]).then(([state, records, digest, prefs, update, history, schedule, discovery]) => {
          if (active) { setData({ ...state, ...records, digest, preferences: prefs, history: history.digests, schedule, discovery }); setJob(update); }
        }).catch((e) => { if (active) setError(e.message); })
          .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
      }, [generation, tab, filters, offset, day]);
      useEffect(() => {
        if (!["queued", "running"].includes(job.status)) return;
        let active = true;
        const timer = setInterval(() => api("update-status", { job_id: job.id }).then((value) => {
          if (!active) return;
          setJob(value);
          if (!["queued", "running"].includes(value.status)) setGeneration((n) => n + 1);
        }).catch((e) => { if (active) setError(e.message); }), 1500);
        return () => { active = false; clearInterval(timer); };
      }, [job.id, job.status]);
      const perform = async (method, payload, message) => {
        setBusy(true); setError("");
        try { const value = await api(method, payload); setNotice(message || "已保存"); setGeneration((n) => n + 1); return value; }
        catch (e) { setError(e.message); }
        finally { setBusy(false); }
      };
      const update = async (source) => {
        const value = await perform("update", source ? { source_ids: [source] } : {}, "更新已触发，进度会自动刷新。");
        if (value) setJob(value);
      };
      const sources = data?.sources ?? [];
      const running = ["queued", "running"].includes(job.status);
      const eventCard = (event, item) => h("article", { key: event.id, className: "document" },
        h("small", null, `${item ? sections[item.section] + " · " : ""}${statuses[event.status] || event.status}`),
        h("h3", null, event.title),
        item && h("p", null, item.reasons.join("；")),
        h("dl", null,
          h("dt", null, "活动时间"), h("dd", null, event.starts_at ? time(event.starts_at) + (event.ends_at ? " — " + time(event.ends_at) : "（结束时间未知）") : "未确认"),
          h("dt", null, "截止时间"), h("dd", null, event.registration_deadline ? time(event.registration_deadline) : "未确认"),
          h("dt", null, "地点"), h("dd", null, event.location || "未确认"),
          h("dt", null, "原文受众"), h("dd", null, event.audience || "未确认"),
          h("dt", null, "参加资格"), h("dd", null, event.eligibility || "未确认"),
          h("dt", null, "首次发现"), h("dd", null, time(event.first_seen_at))),
        h("p", null, ...(event.sources ?? []).map((source) => h("span", { key: source.document_id }, anchor(source.url, source.source_name), " "))),
        event.conflicts && Object.keys(event.conflicts).length > 0 && h("p", { className: "notice" }, "来源字段存在冲突，请打开详情核对各出处。"),
        h("button", { disabled: busy, "aria-pressed": data?.favorites?.includes(event.id) ?? false, onClick: () => perform("favorite", { event_id: event.id, enabled: !data?.favorites?.includes(event.id) }) }, data?.favorites?.includes(event.id) ? "取消收藏" : "收藏"), " ",
        h("label", { style: { display: "inline-flex" } }, "反馈", h("select", { value: data?.feedback?.[event.id] || "clear", disabled: busy, onChange: (e) => perform("feedback", { event_id: event.id, value: e.target.value }) },
          h("option", { value: "clear" }, "未反馈"), h("option", { value: "interested" }, "感兴趣"), h("option", { value: "not_interested" }, "不感兴趣"), h("option", { value: "known" }, "已经知道"))), " ",
        h("button", { disabled: busy, onClick: async () => { try { const detail = await api("detail", { event_id: event.id }); setDetails((all) => ({ ...all, [event.id]: detail })); } catch (e) { setError(e.message); } } }, "查看详情与证据"),
        details[event.id] && h("details", { open: true }, h("summary", null, "原文、字段证据与变更历史"),
          ...details[event.id].documents.map((doc) => h("section", { key: doc.id }, h("h4", null, anchor(doc.url, doc.title)), h("p", null, `发布时间：${doc.published_at || "未知"} · 获取：${time(doc.fetched_at)}`), h("p", { className: "body" }, doc.body))),
          h("pre", null, JSON.stringify({ fields: details[event.id].field_evidence, conflicts: details[event.id].conflicts, changes: details[event.id].changes }, null, 2))));
      const searchForm = () => h("form", { key: tab + JSON.stringify(filters), onSubmit: (e) => { e.preventDefault(); const values = Object.fromEntries(new FormData(e.currentTarget)); setFilters(Object.fromEntries(Object.entries(values).filter(([, value]) => value))); setOffset(0); } },
        h("label", null, "关键词", h("input", { name: "query", defaultValue: filters.query || "", placeholder: "标题或正文" })),
        h("label", null, "来源", h("select", { name: "source_id", defaultValue: filters.source_id || "" }, h("option", { value: "" }, "全部"), ...sources.map((s) => h("option", { key: s.id, value: s.id }, s.name)))),
        h("label", null, "类别", h("select", { name: "category", defaultValue: filters.category || "" }, h("option", { value: "" }, "全部"), ...[...new Set(sources.flatMap((s) => s.categories || [s.category]).filter(Boolean))].map((c) => h("option", { key: c, value: c }, c)))),
        tab !== "documents" && h("label", null, "校区", h("select", { name: "campus", defaultValue: filters.campus || "" }, ...[["", "全部"], ["闵行", "闵行"], ["徐汇", "徐汇"], ["黄浦", "黄浦"], ["七宝", "七宝"], ["长宁", "长宁"], ["unknown", "未知"]].map(([v, t]) => h("option", { key: v, value: v }, t)))),
        tab !== "documents" && h("label", null, "日期从", h("input", { name: "date_from", type: "date", defaultValue: filters.date_from || "" })),
        tab !== "documents" && h("label", null, "到", h("input", { name: "date_to", type: "date", defaultValue: filters.date_to || "" })),
        tab !== "documents" && h("label", null, "状态", h("select", { name: "status", defaultValue: filters.status || "" }, h("option", { value: "" }, "全部"), ...Object.entries(statuses).map(([v, t]) => h("option", { key: v, value: v }, t)))),
        h("button", { type: "submit", disabled: loading }, "搜索／筛选"), h("button", { type: "button", onClick: (e) => { e.currentTarget.form.reset(); setFilters({}); setOffset(0); } }, "清除筛选"));
      const paged = ["all", "favorites", "documents"].includes(tab);
      return h("main", { className: "campus-radar", "aria-label": "校园雷达" }, h("style", null, styles),
        h("header", null, h("div", null, h("h1", null, "校园雷达"), h("p", null, "公开信息 · 出处与字段可核对 · 活动时间按上海显示")),
          h("button", { disabled: busy || running, onClick: () => update() }, "更新全部来源"),
          h("button", { disabled: loading, onClick: () => setGeneration((n) => n + 1) }, "重新读取本地库")),
        error && h("p", { role: "alert" }, error), notice && h("p", { role: "status" }, notice),
        h("p", { role: "status", "aria-live": "polite" }, `${labels[job.status] || job.status}${job.total_sources ? ` · ${job.completed_sources}/${job.total_sources} 来源` : ""}${job.current_source ? " · " + (sources.find((s) => s.id === job.current_source)?.name || job.current_source) : ""}`,
          job.ended_at && ` · 完成于 ${time(job.ended_at)} · 新增 ${job.inserted ?? 0}／变更 ${job.updated ?? 0}／未变 ${job.unchanged ?? 0}`),
        ["failed", "partial", "interrupted"].includes(job.status) && h("p", { className: "notice" }, "本次更新有失败或中断，旧数据和收藏仍可查看。详情见来源状态。"),
        h("nav", { "aria-label": "校园雷达功能" }, ...[["digest", "每日简报"], ["all", "全部活动"], ["favorites", "我的收藏"], ["documents", "全部文档"], ["sources", "来源状态"], ["discovery", "在线补漏"], ["preferences", "偏好与定时"]].map(([key, title]) => h("button", { key, "aria-pressed": tab === key, onClick: () => { setTab(key); setOffset(0); setFilters({}); } }, title))),
        loading && h("p", { role: "status" }, "正在读取本地数据库…"), !data && !loading && h("p", null, "尚未取得数据库结果"),
        data && tab === "digest" && h("section", null, h("h2", null, "每日简报"),
          h("label", null, "简报日期", h("select", { value: day, onChange: (e) => setDay(e.target.value) }, h("option", { value: "" }, "今日"), ...(data.history || []).map((d) => h("option", { key: d.day, value: d.day }, d.day)))),
          h("p", null, `${data.digest.day} · ${data.digest.items.length} 条 · 生成于 ${time(data.digest.generated_at)}`),
          !day && h("button", { disabled: busy, onClick: () => perform("digest", { refresh: true }, "今日简报已重新生成") }, "重新生成今日简报"),
          data.digest.items.length === 0 && h("p", null, "当前没有符合规则的新条目。可以查看全部活动和来源状态。"),
          ...data.digest.items.map((item) => eventCard(item.event, item))),
        data && paged && h("section", null, h("h2", null, tab === "favorites" ? "我的收藏" : tab === "documents" ? "全部已采集文档" : "全部活动（不受偏好隐藏）"), searchForm(),
          h("p", null, `共 ${data.total} 条；本页 ${tab === "documents" ? data.documents?.length ?? 0 : data.events?.length ?? 0} 条`),
          data.total === 0 && h("p", null, "没有匹配的本地记录；可清除筛选查看完整列表。"),
          ...(tab === "documents" ? (data.documents || []).map((doc) => h("article", { key: doc.id, className: "document" }, h("h3", null, anchor(doc.url, doc.title)), h("p", null, `${doc.source_name} · ${doc.processing_status === "manual_reference" ? "非自动采集核对样本" : doc.processing_status} · 识别 ${doc.events?.length ?? 0} 个活动/事项`), h("details", null, h("summary", null, "查看原文抽取内容与证据"), h("p", { className: "body" }, doc.body), h("pre", null, JSON.stringify(doc.evidence, null, 2))))) : (data.events || []).map((event) => eventCard(event))),
          h("button", { disabled: loading || offset === 0, onClick: () => setOffset(Math.max(0, offset - 50)) }, "上一页"), " ", h("button", { disabled: loading || data.next_offset === null, onClick: () => setOffset(data.next_offset) }, "下一页")),
        data && tab === "sources" && h("section", null, h("h2", null, "来源状态与采集范围"), h("div", { className: "sources" }, ...sources.map((source) => {
          const run = data.runs?.find((r) => r.source_id === source.id);
          const supported = source.visibility === "public" && ["sjtu_news", "sjtu_me", "ctld", "weread_latest"].includes(source.adapter);
          return h("section", { key: source.id, className: "source" }, h("strong", null, source.name),
            h("p", null, supported ? source.enabled ? "采集已启用" : "采集已停用" : `尚未支持持续采集 · ${source.status || "候选"}`),
            h("p", null, run ? `${labels[run.status] || run.status} · 取得 ${run.document_count} 篇／新增 ${run.inserted_count}／变更 ${run.updated_count}` : "尚未采集"),
            h("p", null, `最近请求成功：${time(source.last_success_at)}`), h("p", null, source.coverage),
            run?.coverage?.collection_pending && h("p", { className: "notice" }, `仍待补扫 ${run.coverage.pending_list_pages?.length ?? 0} 页／${run.coverage.pending_documents ?? 0} 篇；本轮成功不代表覆盖完成。`),
            source.last_error && h("p", { className: "notice" }, String(source.last_error)),
            run && h("details", null, h("summary", null, "本次范围与错误"), h("pre", null, JSON.stringify({ coverage: run.coverage, errors: run.errors }, null, 2))),
            supported && h("button", { disabled: busy || running || !source.enabled, onClick: () => update(source.id) }, "更新此来源"), " ",
            supported && h("button", { disabled: busy || running, onClick: () => perform("source-toggle", { source_id: source.id, enabled: !source.enabled }) }, source.enabled ? "停用采集" : "启用采集"));
        }))),
        data && tab === "preferences" && h("section", null, h("h2", null, "关注偏好"), h("p", null, "偏好用于简报选择；全部活动与来源采集范围不随偏好缩小。"),
          h("form", { key: generation, onSubmit: (e) => { e.preventDefault(); const values = new FormData(e.currentTarget); perform("preferences-set", { themes: String(values.get("themes")).split(/[,，\n]/).map((s) => s.trim()).filter(Boolean), source_ids: values.getAll("source_ids") }); } },
            h("label", null, "主题（逗号分隔）", h("input", { name: "themes", defaultValue: data.preferences.themes.join("，") })),
            h("label", null, "关注来源（可多选）", h("select", { name: "source_ids", multiple: true, size: 6, defaultValue: data.preferences.source_ids }, ...sources.map((s) => h("option", { key: s.id, value: s.id }, s.name)))),
            h("button", { type: "submit", disabled: busy }, "保存偏好")),
          h("h2", null, "每日自动更新"),
          h("p", null, "手动更新始终可用。定时更新在本机已登录时后台运行，关闭 DSH 仍可执行；休眠或离线期间无法准点获取，恢复后检查补跑。"),
          data.schedule && h("form", { key: "schedule-" + generation, onSubmit: (e) => { e.preventDefault(); const values = new FormData(e.currentTarget); const [hour, minute] = String(values.get("clock")).split(":").map(Number); perform("schedule-set", { enabled: values.get("enabled") === "true", hour, minute, timezone: String(values.get("timezone")) }, "每日更新设置已保存"); } },
            h("label", null, "每日更新", h("select", { name: "enabled", defaultValue: String(data.schedule.enabled) }, h("option", { value: "true" }, "启用"), h("option", { value: "false" }, "停用"))),
            h("label", null, "更新时间", h("input", { name: "clock", type: "time", required: true, defaultValue: String(data.schedule.hour).padStart(2, "0") + ":" + String(data.schedule.minute).padStart(2, "0") })),
            h("label", null, "更新时间区", h("select", { name: "timezone", defaultValue: data.schedule.timezone }, h("option", { value: "Asia/Shanghai" }, "上海 Asia/Shanghai"), h("option", { value: "America/New_York" }, "纽约 America/New_York"))),
            h("button", { type: "submit", disabled: busy }, "保存每日更新时间")),
          data.schedule && h("p", null, `后台触发器：${data.schedule.runtime?.installed_at ? "已安装" : "尚未安装"} · 最近检查：${time(data.schedule.state?.last_tick_at)} · 下次计划：${data.schedule.state?.next_due_at || "待检查"}`)),
        data && tab === "discovery" && h("section", null, h("h2", null, "在线补漏与手工链接"),
          h("p", null, "在线检索当前覆盖教学发展中心官网活动栏目。全网自动搜索尚不可用；公众号可保存文章链接并尝试读取公开正文，持续订阅尚未接入。"),
          h("form", { onSubmit: (e) => { e.preventDefault(); perform("discover-search", { query: String(new FormData(e.currentTarget).get("query")) }, "官网检索已完成；候选链接尚未获取正文"); } },
            h("label", null, "官网检索关键词", h("input", { name: "query", required: true, maxLength: 100, placeholder: "例如 AI、征稿" })), h("button", { type: "submit", disabled: busy }, "检索官网")),
          h("form", { onSubmit: (e) => { e.preventDefault(); perform("candidate-add", { url: String(new FormData(e.currentTarget).get("url")) }, "链接已保存，尚未获取正文"); } },
            h("label", null, "文章链接", h("input", { name: "url", type: "url", required: true, placeholder: "交大官网或公众号文章 URL" })), h("button", { type: "submit", disabled: busy }, "保存候选链接")),
          data.discovery?.last_search && h("p", { role: "status" }, data.discovery.last_search.status === "success" ? `最近检索“${data.discovery.last_search.query}”：${data.discovery.last_search.result_count} 个链接；正文读取另行操作。` : `最近检索失败：${data.discovery.last_search.error}`),
          ...(data.discovery?.candidates || []).map((candidate) => h("article", { key: candidate.url, className: "document" }, h("h3", null, anchor(candidate.url, candidate.title || candidate.url)),
            h("p", null, `${{ candidate: "候选链接／未取得正文", fetched: "正文已入库", failed: "正文获取失败／链接保留", refresh_failed: "本次刷新失败／旧正文保留" }[candidate.status] || candidate.status} · 来自 ${candidate.origin}`),
            candidate.error && h("p", { className: "notice" }, candidate.error),
            h("button", { disabled: busy, onClick: () => perform("candidate-fetch", { url: candidate.url }, "正文获取已返回，请查看该链接的状态") }, "获取公开正文")))));
    }
    function Icon() { return h("svg", { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, "aria-hidden": true }, h("circle", { cx: 12, cy: 12, r: 8 }), h("circle", { cx: 12, cy: 12, r: 4 }), h("path", { d: "M12 12L19 5" })); }
    return { inject: ["slots", "connection"], apply(ctx) {
      ctx.slots.inject("main", () => ctx.slots.register({ name: "main", key: "campus-radar", inject: () => ({ call: (method, payload = {}) => ctx.connection.rpc.call("/api", `campus-radar/${method}`, payload) }) }, Panel));
      ctx.slots.inject("sidebar.panellist", () => ctx.slots.register({ name: "sidebar.panellist", id: "campus-radar", order: 20, label: "校园雷达" }, Icon));
    } };
  },
});
