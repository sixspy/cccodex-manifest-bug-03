(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let reportDetail = null;
  let latestReport = null;
  let waterline = null;
  let reconcileResult = null;
  let asOfDate = "2026-11-16";
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    waterline = await api("/api/visibility-waterline?as_of_date=" + asOfDate);
    if (reports.length) {
      const latest = await api("/api/visibility-reports/" + reports[0].id);
      latestReport = latest.report;
    } else {
      latestReport = null;
    }
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function openReport(id) {
    const r = await api("/api/visibility-reports/" + id);
    reportDetail = r.report;
    tab = "report-detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: asOfDate }),
      });
      reportDetail = r.report;
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReconcile() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reconcile", {
        method: "POST",
        body: JSON.stringify({ as_of_date: asOfDate, rounds: 2 }),
      });
      reconcileResult = r.result;
      await refresh();
      tab = "reconcile";
      syncNav();
      setMsg("复跑两轮一致，对账通过");
      render();
    } catch (e) {
      reconcileResult = null;
      setMsg(e.message);
      render();
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      const active =
        btn.dataset.tab === tab ||
        (btn.dataset.tab === "reports" && tab === "report-detail");
      btn.classList.toggle("active", active);
    });
  }

  function deltaLabel(r) {
    const d = Number(r.mismatch_delta || 0);
    if (d > 0) return `<span class="delta up">+${d}</span>`;
    if (d < 0) return `<span class="delta down">${d}</span>`;
    return `<span class="delta flat">0</span>`;
  }

  function statePill(state) {
    const cls = state === "新增" ? "up" : state === "已消除" ? "down" : "flat";
    return `<span class="pill state ${cls}">${esc(state)}</span>`;
  }

  function mismatchRows(items) {
    if (!items || !items.length) {
      return `<tr><td colspan="8" class="muted">本报告无错位行</td></tr>`;
    }
    return items
      .map(
        (m) => `<tr>
          <td>${esc(m.batch_id)}</td>
          <td>${esc(m.zone_name)}<div class="muted">${esc(m.zone_code)}</div></td>
          <td>${esc(m.biz_date)}</td>
          <td>${esc(m.manifest_object_count)}</td>
          <td>${esc(m.physical_object_count)}</td>
          <td>${Number(m.delta) > 0 ? "+" : ""}${esc(m.delta)}</td>
          <td>${statePill(m.state)}</td>
          <td class="muted">${m.state === "新增" ? "本次新出现" : m.state === "沿用" ? "上轮已有" : "上轮有、本轮已消"}</td>
        </tr>`
      )
      .join("");
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th>清单数</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.zone_name)}<div class="muted">${esc(b.zone_code)}</div></td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="pill">${esc(b.status)}</span></td>
            <td>${b.closed_object_count == null ? "—" : esc(b.closed_object_count)}</td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${esc(b.zone_name)} · ${esc(b.biz_date)} · <span class="pill">${esc(b.status)}</span>
        · 清单数 ${b.closed_object_count == null ? "—" : esc(b.closed_object_count)}</p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${esc(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${esc(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${esc(form.byte_size)}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${esc(o.object_key)}</td><td>${esc(o.content_hash)}</td><td>${esc(o.byte_size)}</td><td>${esc(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReports() {
    const latest = latestReport || reportDetail || null;
    return `
      <h2>可见性对账 · 错位列表</h2>
      <div class="row-actions">
        <label class="inline">截至<input id="f-asof" value="${esc(asOfDate)}" /></label>
        <button type="button" id="btn-report">重算</button>
      </div>
      <p class="muted">重算为整笔替换：同一截至日旧摘要与旧错位先清再写，旧错位不跨报告悬挂。</p>
      ${
        latest
          ? `<div class="panel">
        <h3>报告 #${latest.id} · 截至 ${esc(latest.as_of_date)} · 错位 ${esc(latest.mismatch_count)}</h3>
        <p>${esc(latest.summary)}</p>
        <p class="muted">
          上轮错位 ${esc(latest.prev_mismatch_count)}，本轮 ${
            latest.mismatch_delta >= 0 ? "+" : ""
          }${esc(latest.mismatch_delta)}
          （新增 ${esc(latest.new_mismatch_count)} · 沿用 ${esc(latest.carried_mismatch_count)} · 已消除 ${esc(latest.cleared_mismatch_count)}）
        </p>
        <table>
          <thead><tr><th>来源批</th><th>湖区</th><th>业务日</th><th>清单数</th><th>实物数</th><th>偏差</th><th>新旧</th><th>说明</th></tr></thead>
          <tbody>${mismatchRows(latest.mismatches)}</tbody>
        </table>
      </div>`
          : `<p class="muted">尚无对账报告，点“重算”生成第一份。</p>`
      }
      <h3>报告详情列表</h3>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>较上轮</th><th>摘要</th><th></th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${esc(r.as_of_date)}</td><td>${esc(r.mismatch_count)}</td>
            <td>${deltaLabel(r)}</td><td>${esc(r.summary)}</td>
            <td><button type="button" class="secondary" data-report="${r.id}">详情</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReportDetail() {
    if (!reportDetail) {
      return `<h2>报告详情</h2><p class="muted">请先从对账列表打开一份报告。</p>`;
    }
    const r = reportDetail;
    const items = r.mismatches || [];
    return `
      <h2>报告详情 #${r.id}</h2>
      <p>截至 ${esc(r.as_of_date)} · 已闭合 ${esc(r.closed_batch_count)} 批 · 收集中 ${esc(
      r.open_batch_count
    )} 批 · 对象 ${esc(r.object_count)} · 孤儿 ${esc(r.orphan_object_count)} · 错位 ${esc(
      r.mismatch_count
    )}</p>
      <p class="muted">
        较上轮 ${
          r.mismatch_delta >= 0 ? "+" : ""
        }${esc(r.mismatch_delta)}：新增 ${esc(r.new_mismatch_count)} · 沿用 ${esc(
      r.carried_mismatch_count
    )} · 已消除 ${esc(r.cleared_mismatch_count)}
      </p>
      <h3>错位明细（含来源批与新旧差）</h3>
      <table>
        <thead><tr><th>来源批</th><th>湖区</th><th>业务日</th><th>清单数</th><th>实物数</th><th>偏差</th><th>新旧</th><th>说明</th></tr></thead>
        <tbody>${mismatchRows(items)}</tbody>
      </table>
      <p class="muted">${esc(r.summary)}</p>`;
  }

  function renderWaterline() {
    const w = waterline || { items: [] };
    const items = w.items || [];
    return `
      <h2>批次水线</h2>
      <div class="row-actions">
        <label class="inline">水线日<input id="f-waterline" value="${esc(asOfDate)}" /></label>
        <button type="button" class="secondary" id="btn-waterline">查看</button>
      </div>
      <p class="muted">水线 ${esc(w.watermark || "—")} · 错位 ${esc(w.mismatch_count == null ? "—" : w.mismatch_count)}
        （清单数与实物数须对齐，偏了重算即停）</p>
      <table>
        <thead><tr><th>批次</th><th>湖区</th><th>业务日</th><th>状态</th><th>清单数</th><th>实物数</th><th>偏差</th><th>对齐</th></tr></thead>
        <tbody>
          ${items
            .map(
              (i) => `<tr>
            <td>${esc(i.batch_id)}</td>
            <td>${esc(i.zone_name)}<div class="muted">${esc(i.zone_code)}</div></td>
            <td>${esc(i.biz_date)}</td>
            <td><span class="pill">${esc(i.status)}</span></td>
            <td>${i.manifest_object_count == null ? "—" : esc(i.manifest_object_count)}</td>
            <td>${esc(i.physical_object_count)}</td>
            <td>${i.delta > 0 ? "+" : ""}${esc(i.delta)}</td>
            <td>${i.aligned ? '<span class="pill">对齐</span>' : '<span class="pill state down">偏</span>'}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderReconcile() {
    const r = reconcileResult;
    return `
      <h2>对账复跑台</h2>
      <div class="row-actions">
        <label class="inline">截至<input id="f-rec-asof" value="${esc(asOfDate)}" /></label>
        <button type="button" id="btn-reconcile">复跑两轮</button>
      </div>
      <p class="muted">连跑两轮，摘要、错位、水线数字须逐项一致；不一致即停，不落库。</p>
      ${
        r
          ? `<div class="panel">
        <h3>复跑对照 · 截至 ${esc(r.as_of_date)} · ${r.stable ? "一致" : "不一致"}</h3>
        <table>
          <thead><tr><th>轮次</th><th>已闭合</th><th>收集中</th><th>对象</th><th>孤儿</th><th>错位</th></tr></thead>
          <tbody>
            ${(r.runs || [])
              .map(
                (x, i) => `<tr>
              <td>第 ${i + 1} 轮</td>
              <td>${esc(x.closed_batch_count)}</td>
              <td>${esc(x.open_batch_count)}</td>
              <td>${esc(x.object_count)}</td>
              <td>${esc(x.orphan_object_count)}</td>
              <td>${esc(x.mismatch_count)}</td>
            </tr>`
              )
              .join("")}
            <tr class="strong">
              <td>落库摘要</td>
              <td>${esc(r.counters.closed_batch_count)}</td>
              <td>${esc(r.counters.open_batch_count)}</td>
              <td>${esc(r.counters.object_count)}</td>
              <td>${esc(r.counters.orphan_object_count)}</td>
              <td>${esc(r.counters.mismatch_count)}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">落库报告 #${esc(r.report_id)} · ${esc(r.summary)}</p>
        <p class="muted">水线 ${esc((r.waterline && r.waterline.watermark) || "—")} · 错位 ${esc(
              (r.waterline && r.waterline.mismatch_count) == null
                ? "—"
                : r.waterline.mismatch_count
            )}</p>
      </div>`
          : `<p class="muted">尚未复跑。</p>`
      }`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "report-detail") view.innerHTML = renderReportDetail();
    else if (tab === "waterline") view.innerHTML = renderWaterline();
    else if (tab === "reconcile") view.innerHTML = renderReconcile();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    view.querySelectorAll("[data-report]").forEach((btn) => {
      btn.onclick = () => openReport(Number(btn.dataset.report));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
    const asof = document.getElementById("f-asof");
    if (asof) asof.oninput = () => (asOfDate = asof.value);
    const wl = document.getElementById("btn-waterline");
    if (wl)
      wl.onclick = async () => {
        const input = document.getElementById("f-waterline");
        asOfDate = input ? input.value : asOfDate;
        try {
          waterline = await api("/api/visibility-waterline?as_of_date=" + asOfDate);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    const rec = document.getElementById("btn-reconcile");
    if (rec)
      rec.onclick = async () => {
        const input = document.getElementById("f-rec-asof");
        asOfDate = input ? input.value : asOfDate;
        doReconcile();
      };
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      if (tab === "report-detail") tab = "reports";
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
