class ActivitySourcePicker {
  constructor(onChange) {
    this.onChange = onChange;
    this.rows = [];
    this.columns = [];
    this.selected = new Set();
    this.document = "";
    this.sheet = "";
    this.page = 0;
    this.pageSize = 50;
    this.loading = false;
    this.running = false;
    this.saving = false;
    this.savedDocs = [];
    this.hidden = new Set();
    this.showHidden = false;
    this.hiddenStorageKey = "activity-wps-hidden-v1";
    this.filters = [];
    this.filterMode = "all";
    this.element = (id) => document.getElementById(id);
  }

  escape(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  }

  async init() {
    this.element("loadSourceDoc").addEventListener("click", () => this.load());
    this.element("saveSourceDoc").addEventListener("click", () => this.saveDocument());
    this.element("refreshSource").addEventListener("click", () => this.load(this.sheet));
    this.element("sourceUrl").addEventListener("input", () => {
      this.syncSavedDocument();
      this.element("sourceSaveStatus").textContent = "";
      this.invalidate(true);
    });
    this.element("sourceName").addEventListener("input", () => {
      this.element("sourceSaveStatus").textContent = "";
    });
    this.element("sourceUrl").addEventListener("keydown", (event) => {
      if (event.key === "Enter") this.load();
    });
    this.element("sourceDoc").addEventListener("change", (event) => {
      this.element("sourceUrl").value = event.target.value;
      this.syncSavedDocument();
      this.element("sourceSaveStatus").textContent = "";
      this.invalidate(true);
      if (event.target.value) this.load();
    });
    this.element("selSheet").addEventListener("change", (event) => this.load(event.target.value));
    this.element("sourceFilter").addEventListener("input", () => {
      this.page = 0;
      this.render();
    });
    this.element("filterField").addEventListener("change", () => this.updateOperators());
    this.element("filterOperator").addEventListener("change", () => this.updateFilterInput());
    this.element("filterValue").addEventListener("keydown", (event) => { if (event.key === "Enter") this.addFilter(); });
    this.element("addFilter").addEventListener("click", () => this.addFilter());
    this.element("filterMode").addEventListener("change", (event) => { this.filterMode = event.target.value; this.render(); });
    this.element("filterChips").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-filter-index]");
      if (!button) return;
      this.filters.splice(Number(button.dataset.filterIndex), 1);
      this.render();
    });
    this.element("sourcePrev").addEventListener("click", () => { this.page -= 1; this.render(); });
    this.element("sourceNext").addEventListener("click", () => { this.page += 1; this.render(); });
    this.element("clearSourceSelection").addEventListener("click", () => {
      this.selected.clear();
      this.render();
    });
    this.element("toggleHiddenSource").addEventListener("click", () => {
      this.showHidden = !this.showHidden;
      this.element("toggleHiddenSource").textContent = this.showHidden ? "隐藏已隐藏项" : "显示已隐藏";
      this.page = 0;
      this.render();
    });
    this.element("selectSourceFiltered").addEventListener("click", () => {
      this.filtered().filter((row) => row.selectable).forEach((row) => this.selected.add(row.spu));
      this.render();
    });
    this.element("sourceBody").addEventListener("change", (event) => {
      const checkbox = event.target.closest("input[data-spu]");
      if (!checkbox || this.running || this.loading) return;
      if (checkbox.checked) this.selected.add(checkbox.dataset.spu);
      else this.selected.delete(checkbox.dataset.spu);
      this.render();
    });
    this.element("sourceBody").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-hide-row]");
      if (!button || this.running || this.loading) return;
      const key = button.dataset.hideRow;
      this.hidden.add(key);
      this.selected.delete(button.dataset.spu || "");
      this.saveHidden();
      this.render();
    });
    this.render();
    try {
      await this.refreshSavedDocuments();
    } catch (error) {
      this.showError(error.message);
    }
    try {
      const saved = JSON.parse(localStorage.getItem("activity-wps-source") || "null");
      if (saved?.cloud_url && !this.document && !this.loading && !this.element("sourceUrl").value) {
        this.element("sourceUrl").value = saved.cloud_url;
        await this.load(saved.sheet || "");
      }
    } catch (error) {
      this.element("sourceStatus").textContent = "请选择金山文档。";
    }
  }

  syncSavedDocument() {
    const url = this.element("sourceUrl").value.trim();
    const saved = this.savedDocs.find((entry) => entry.url === url);
    this.element("sourceDoc").value = saved ? url : "";
    this.element("sourceName").value = saved?.name || "";
  }

  async refreshSavedDocuments() {
    const response = await fetch("/api/cloud_docs", { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取已保存文档，可直接粘贴链接。");
    const data = await response.json();
    this.savedDocs = data.docs || [];
    const selector = this.element("sourceDoc");
    selector.replaceChildren(new Option("选择文档或粘贴新链接", ""));
    for (const entry of this.savedDocs) selector.add(new Option(entry.name || entry.url, entry.url));
    this.syncSavedDocument();
  }

  async saveDocument() {
    if (this.saving || this.loading || this.running) return;
    const url = this.element("sourceUrl").value.trim();
    const name = this.element("sourceName").value.trim();
    const status = this.element("sourceSaveStatus");
    status.className = "small mt-1 text-muted";
    try {
      const parsed = new URL(url);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password
        || !/(^|\.)(kdocs|wps)\.cn$/i.test(parsed.hostname)) {
        throw new Error("请填写有效的金山 WPS 在线表格链接。");
      }
    } catch (error) {
      status.className = "small mt-1 text-danger";
      status.textContent = "请填写有效的金山 WPS 在线表格链接。";
      return;
    }
    this.saving = true;
    status.textContent = "正在保存…";
    this.render();
    try {
      const response = await fetch("/api/cloud_docs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, name }),
      });
      const data = await response.json();
      if (!response.ok || data.status !== "success") throw new Error(data.detail || data.error || "保存失败，请重试。");
      await this.refreshSavedDocuments();
      const saved = this.savedDocs.find((entry) => entry.url === url);
      if (!saved || (name && saved.name !== name)) throw new Error("未确认链接已保存，请重试。");
      status.className = "small mt-1 text-success";
      status.textContent = `已保存「${saved.name || saved.url}」，下次可从文档列表选择。`;
    } catch (error) {
      status.className = "small mt-1 text-danger";
      status.textContent = error.message;
      this.element("sourceName").value = name;
    } finally {
      this.saving = false;
      this.render();
    }
  }

  invalidate(resetSheets = false) {
    this.rows = [];
    this.columns = [];
    this.filters = [];
    this.selected.clear();
    this.page = 0;
    this.sheet = "";
    this.hidden = new Set();
    this.element("sourceFilter").value = "";
    this.element("sourceWarning").classList.add("d-none");
    this.element("openSourceDoc").classList.add("d-none");
    this.element("sourceStatus").textContent = "请选择文档与工作表后读取数据。";
    if (resetSheets) {
      this.document = "";
      this.element("selSheet").replaceChildren(new Option("— 请先读取文档 —", ""));
    }
    this.render();
  }

  showError(message) {
    this.element("sourceError").textContent = message;
    this.element("sourceError").classList.toggle("d-none", !message);
  }

  async load(sheet = "") {
    if (this.loading || this.running || this.saving) return;
    const cloudUrl = this.element("sourceUrl").value.trim();
    this.invalidate(!sheet);
    this.showError("");
    if (!cloudUrl) {
      this.showError("请先选择文档或粘贴 WPS 在线表格链接。");
      return;
    }
    this.loading = true;
    this.element("sourceStatus").textContent = sheet ? "正在读取工作表数据，请稍候…" : "正在读取文档的工作表…";
    this.render();
    try {
      const query = new URLSearchParams({ cloud_url: cloudUrl, sheet });
      const response = await fetch("/activity/worklist?" + query);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "读取失败，请检查文档访问权限与登录状态。");
      this.document = data.cloud_url;
      this.sheet = data.sheet;
      this.loadHidden();
      this.rows = data.rows || [];
      const priority = ["spu", "sku", "daily", "sale", "purchase", "category"]
        .map((field) => data.fields?.[field]).filter(Boolean);
      this.columns = [...(data.columns || [])].sort((first, second) => {
        const firstIndex = priority.indexOf(first.key);
        const secondIndex = priority.indexOf(second.key);
        return (firstIndex < 0 ? priority.length : firstIndex) - (secondIndex < 0 ? priority.length : secondIndex);
      });
      this.setupFilterFields();
      const sheetSelect = this.element("selSheet");
      sheetSelect.replaceChildren(new Option("— 请选择工作表 —", ""));
      for (const name of data.sheets || []) sheetSelect.add(new Option(name, name));
      sheetSelect.value = this.sheet;
      const draftName = this.element("sourceName").value;
      try {
        await this.refreshSavedDocuments();
      } catch (error) {
        this.element("sourceSaveStatus").textContent = "文档已读取，已保存文档列表暂未刷新。";
      }
      if (draftName) this.element("sourceName").value = draftName;
      this.element("openSourceDoc").href = this.document;
      this.element("openSourceDoc").classList.remove("d-none");
      this.element("sourceWarning").textContent = (data.warnings || []).join(" ");
      this.element("sourceWarning").classList.toggle("d-none", !(data.warnings || []).length);
      const loadedAt = new Date(data.loaded_at).toLocaleTimeString();
      this.element("sourceStatus").textContent = this.sheet
        ? `已读取 ${data.total || 0} 行 · 可选择 ${data.selectable || 0} 个 SPU · 更新于 ${loadedAt}`
        : `文档已连接，共 ${data.sheets.length} 个工作表，请选择工作表。`;
      try {
        localStorage.setItem("activity-wps-source", JSON.stringify({ cloud_url: this.document, sheet: this.sheet }));
      } catch (error) {
        this.element("sourceStatus").textContent += " · 浏览器未保存本次选择";
      }
    } catch (error) {
      this.showError(error.message);
      this.element("sourceStatus").textContent = "读取失败，请检查链接、kdocs-cli 登录状态或文档访问权限后重试。";
    } finally {
      this.loading = false;
      this.render();
    }
  }

  filtered() {
    const keyword = this.element("sourceFilter").value.trim().toLowerCase();
    const rows = this.showHidden ? this.rows : this.rows.filter((row) => !this.hidden.has(this.rowKey(row)));
    const textRows = keyword ? rows.filter((row) => Object.values(row.values).some(
      (value) => String(value).toLowerCase().includes(keyword),
    )) : rows;
    if (!this.filters.length) return textRows;
    return textRows.filter((row) => {
      const checks = this.filters.map((filter) => this.matchFilter(row, filter));
      return this.filterMode === "any" ? checks.some(Boolean) : checks.every(Boolean);
    });
  }

  isNumericField(key) {
    return ["daily", "sale", "purchase", "weight", "discount", "ros"].includes(key);
  }

  setupFilterFields() {
    const select = this.element("filterField");
    select.replaceChildren(new Option("字段", ""));
    for (const column of this.columns) {
      const key = column.key;
      select.add(new Option(column.title, key));
    }
    select.disabled = !this.columns.length || this.loading || this.running;
    this.updateOperators();
  }

  updateOperators() {
    const key = this.element("filterField").value;
    const numeric = this.isNumericField(key);
    const operators = numeric
      ? [["eq", "等于"], ["gte", "≥"], ["lte", "≤"], ["gt", ">"], ["lt", "<"]]
      : [["contains", "包含"], ["not_contains", "不包含"], ["eq", "等于"], ["starts", "开头是"]];
    const select = this.element("filterOperator");
    select.replaceChildren(...operators.map(([value, label]) => new Option(label, value)));
    select.disabled = !key || this.loading || this.running;
    this.updateFilterInput();
  }

  updateFilterInput() {
    const numeric = this.isNumericField(this.element("filterField").value);
    const input = this.element("filterValue");
    input.type = numeric ? "number" : "text";
    input.step = "any";
    input.placeholder = numeric ? "输入数字" : "输入文字（支持模糊匹配）";
    input.disabled = !this.element("filterField").value || this.loading || this.running;
    this.element("addFilter").disabled = input.disabled;
  }

  addFilter() {
    const key = this.element("filterField").value;
    const value = this.element("filterValue").value.trim();
    if (!key || !value) return;
    const column = this.columns.find((item) => item.key === key);
    this.filters.push({ key, label: column?.title || key, operator: this.element("filterOperator").value, value });
    this.element("filterValue").value = "";
    this.page = 0;
    this.render();
  }

  matchFilter(row, filter) {
    const raw = row.values[filter.key] ?? "";
    if (this.isNumericField(filter.key)) {
      const actual = Number(String(raw).replace(/[%￥¥$,元\s]/g, ""));
      const expected = Number(filter.value);
      if (!Number.isFinite(actual) || !Number.isFinite(expected)) return false;
      return { eq: actual === expected, gte: actual >= expected, lte: actual <= expected,
        gt: actual > expected, lt: actual < expected }[filter.operator] || false;
    }
    const actual = String(raw).toLowerCase();
    const expected = filter.value.toLowerCase();
    return { contains: actual.includes(expected), not_contains: !actual.includes(expected),
      eq: actual === expected, starts: actual.startsWith(expected) }[filter.operator] || false;
  }

  rowKey(row) {
    return `${this.document}\n${this.sheet}\n${row.row_number}\n${row.spu}`;
  }

  loadHidden() {
    try {
      const stored = JSON.parse(localStorage.getItem(this.hiddenStorageKey) || "{}");
      const values = stored[`${this.document}\n${this.sheet}`] || [];
      this.hidden = new Set(values);
    } catch (error) {
      this.hidden = new Set();
    }
  }

  saveHidden() {
    try {
      const stored = JSON.parse(localStorage.getItem(this.hiddenStorageKey) || "{}");
      const key = `${this.document}\n${this.sheet}`;
      stored[key] = [...this.hidden].slice(-5000);
      localStorage.setItem(this.hiddenStorageKey, JSON.stringify(stored));
    } catch (error) {
      this.element("sourceStatus").textContent = "已隐藏本次项目，但浏览器未能保存隐藏记录。";
    }
  }

  setRunning(running) {
    this.running = running;
    this.render();
  }

  render() {
    const filtered = this.filtered();
    const pageCount = Math.max(1, Math.ceil(filtered.length / this.pageSize));
    this.page = Math.max(0, Math.min(this.page, pageCount - 1));
    const visible = filtered.slice(this.page * this.pageSize, (this.page + 1) * this.pageSize);
    const disabled = this.running || this.loading || this.saving;
    this.element("filterMode").disabled = disabled || !this.filters.length;
    this.element("filterModeLabel").textContent = `组合条件：${this.filterMode === "any" ? "任一满足" : "同时满足"}`;
    this.element("filterChips").innerHTML = this.filters.map((filter, index) =>
      `<span class="badge text-bg-light text-dark border">${this.escape(filter.label)} ${this.escape(filter.operator)} ${this.escape(filter.value)} <button type="button" class="btn-close ms-1" style="font-size:.55rem" data-filter-index="${index}" aria-label="移除筛选条件"></button></span>`).join("");
    this.element("sourceHead").innerHTML = this.columns.length
      ? `<tr><th>选择</th><th>文档行号</th><th>数据状态</th>${this.columns.map(
        (column) => `<th>${this.escape(column.title)}</th>`,
      ).join("")}<th>操作</th></tr>` : "";
    this.element("sourceBody").innerHTML = visible.length ? visible.map((row) => {
      const state = row.selectable ? '<span class="badge text-bg-success">可选择</span>'
        : `<span class="text-warning-emphasis" title="${this.escape(row.issues.join("；"))}">${this.escape(row.issues.join("；"))}</span>`;
      const hidden = this.hidden.has(this.rowKey(row));
      return `<tr class="${hidden ? "table-secondary" : ""}"><td><input class="form-check-input" type="checkbox" data-spu="${this.escape(row.spu)}"
        aria-label="选择第 ${row.row_number} 行商品 ${this.escape(row.spu)}"
        ${this.selected.has(row.spu) ? "checked" : ""} ${disabled || !row.selectable ? "disabled" : ""}></td>
        <td>${row.row_number}</td><td>${state}</td>${this.columns.map((column) => {
          const original = row.values[column.key] || "";
          const value = this.escape(/DISPIMG\s*\(/i.test(original) ? "图片请在原文档查看" : original);
          return `<td title="${value}">${value || "—"}</td>`;
        }).join("")}<td><button type="button" class="btn btn-link btn-sm p-0" data-hide-row="${this.escape(this.rowKey(row))}" data-spu="${this.escape(row.spu)}">${hidden ? "取消隐藏" : "隐藏"}</button></td></tr>`;
    }).join("") : `<tr><td colspan="${this.columns.length + 4}" class="text-center text-muted p-4">${
      this.loading ? "正在读取 WPS 文档…" : this.sheet ? "没有匹配的数据" : "选择文档与工作表后，数据将在这里显示"
    }</td></tr>`;
    this.element("sourcePageInfo").textContent = this.sheet
      ? `第 ${this.page + 1} / ${pageCount} 页 · 筛选后 ${filtered.length} 行 · 每页 ${this.pageSize} 行` : "";
    this.element("sourceSelectionCount").textContent = `已选 ${this.selected.size} 个 SPU · 已隐藏 ${this.hidden.size} 行`;
    for (const control of ["sourceDoc", "sourceUrl", "sourceName", "loadSourceDoc", "selSheet"]) {
      this.element(control).disabled = disabled;
    }
    this.setupFilterFields();
    this.element("saveSourceDoc").disabled = disabled || !this.element("sourceUrl").value.trim();
    this.element("refreshSource").disabled = disabled || !this.document || !this.sheet;
    this.element("sourcePrev").disabled = this.loading || this.page === 0;
    this.element("sourceNext").disabled = this.loading || this.page >= pageCount - 1;
    this.element("selectSourceFiltered").disabled = disabled || !filtered.some((row) => row.selectable);
    this.element("clearSourceSelection").disabled = disabled || !this.selected.size;
    this.onChange({ cloud_url: this.document, sheet: this.sheet, spus: [...this.selected] });
  }
}
