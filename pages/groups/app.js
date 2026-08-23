(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;
  var BATCH_GROUP_LIMIT = 100;
  var groups = [];
  var selected = new Set();
  var identities = { bindings: [], suspicious: [], violations: [] };
  var identityPages = { bindings: 1, suspicious: 1, violations: 1 };
  var identityPageSize = 10;
  var globalKeywordConfig = { groups: [], rules: [] };
  var runtimeSettings = {};
  var bilibiliLoginKey = "";
  var bilibiliLoginTimer;
  var editingGroup;
  var toastTimer;

  function element(id) {
    return document.getElementById(id);
  }

  function unwrap(response) {
    if (response && response.ok === false) {
      throw new Error(response.message || "请求失败");
    }
    return response && Object.prototype.hasOwnProperty.call(response, "data")
      ? response.data
      : response;
  }

  function apiGet(path) {
    return bridge.apiGet(path, {}).then(unwrap);
  }

  function apiPost(path, body) {
    return bridge.apiPost(path, body).then(unwrap);
  }

  function toast(message, danger) {
    var node = element("toast");
    node.textContent = message;
    node.classList.toggle("danger", Boolean(danger));
    node.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      node.classList.remove("visible");
    }, 2600);
  }

  function modeLabel(mode) {
    return {
      off: "已关闭",
      uid: "条件审核",
      conditional: "条件审核",
      native: "QQ 白名单"
    }[mode] || "配置异常";
  }

  function stateLabel(group) {
    if (group.mode === "off") {
      return group.synchronized ? "已关闭" : "待清理";
    }
    return group.synchronized ? "已应用" : "待应用";
  }

  function makeBadge(text, tone) {
    var badge = document.createElement("span");
    badge.className = "badge " + tone;
    badge.textContent = text;
    return badge;
  }

  function actionButton(text, className, handler) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "text-button " + (className || "");
    button.textContent = text;
    button.addEventListener("click", handler);
    return button;
  }

  function visibleGroups() {
    var query = element("search-input").value.trim().toLocaleLowerCase();
    return groups.filter(function (group) {
      return !query || (group.group_name + " " + group.group_openid).toLocaleLowerCase().includes(query);
    });
  }

  function selectedGroupIds() {
    return groups
      .filter(function (group) { return selected.has(group.group_openid); })
      .map(function (group) { return group.group_openid; });
  }

  function updateSelectionControls(visible) {
    var visibleIds = visible.map(function (group) { return group.group_openid; });
    var visibleSelected = visibleIds.filter(function (id) { return selected.has(id); }).length;
    var selectVisible = element("select-visible");
    selectVisible.disabled = visibleIds.length === 0;
    selectVisible.checked = visibleIds.length > 0 && visibleSelected === visibleIds.length;
    selectVisible.indeterminate = visibleSelected > 0 && visibleSelected < visibleIds.length;
    element("selected-count").textContent = selected.size;
    element("batch-toolbar").hidden = selected.size === 0;
  }

  function render() {
    var visible = visibleGroups();
    var tbody = element("group-rows");
    tbody.replaceChildren();

    if (!visible.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = document.createElement("td");
      emptyCell.colSpan = 6;
      emptyCell.className = "empty";
      emptyCell.textContent = groups.length ? "没有匹配的群" : "暂无群配置，请先在群内使用 /审核设置 绑定";
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
      updateSelectionControls(visible);
      return;
    }

    visible.forEach(function (group) {
      var row = document.createElement("tr");
      var selectCell = document.createElement("td");
      var selectGroup = document.createElement("input");
      selectCell.className = "row-select";
      selectGroup.type = "checkbox";
      selectGroup.className = "selection-checkbox";
      selectGroup.checked = selected.has(group.group_openid);
      selectGroup.setAttribute("aria-label", "选择" + (group.group_name || group.group_openid));
      selectGroup.addEventListener("change", function () {
        if (selectGroup.checked && selected.size >= BATCH_GROUP_LIMIT) {
          selectGroup.checked = false;
          toast("单次最多选择 " + BATCH_GROUP_LIMIT + " 个群", true);
        } else if (selectGroup.checked) selected.add(group.group_openid);
        else selected.delete(group.group_openid);
        updateSelectionControls(visibleGroups());
      });
      selectCell.appendChild(selectGroup);
      var identity = document.createElement("td");
      var name = document.createElement("strong");
      var openid = document.createElement("code");
      name.textContent = group.group_name || "待获取群名称";
      openid.textContent = group.group_openid;
      identity.className = "group-identity";
      identity.append(name, openid);

      var mode = document.createElement("td");
      mode.appendChild(makeBadge(modeLabel(group.mode), group.mode === "off" ? "neutral" : "blue"));
      var bound = document.createElement("td");
      bound.appendChild(makeBadge(group.bound ? "已绑定" : "未绑定", group.bound ? "green" : "neutral"));
      var sync = document.createElement("td");
      sync.appendChild(makeBadge(stateLabel(group), group.synchronized ? "green" : "amber"));
      var actions = document.createElement("td");
      actions.className = "row-actions";
      actions.append(
        actionButton("编辑", "", function () { openEditor(group); }),
        actionButton("应用", "primary", function () { syncGroup(group); }),
        actionButton("移除", "danger", function () { removeGroup(group); })
      );
      row.append(selectCell, identity, mode, bound, sync, actions);
      tbody.appendChild(row);
    });
    updateSelectionControls(visible);
  }

  function groupLabel(groupOpenid) {
    var group = groups.find(function (item) { return item.group_openid === groupOpenid; });
    return group && group.group_name ? group.group_name : (groupOpenid || "未知群");
  }

  function recordGroupLabel(record) {
    return record && (record.group_name || record.group) || groupLabel(record && record.group_openid);
  }

  function identityText(value) {
    return value == null || value === "" ? "-" : String(value);
  }

  function identityGroups(binding) {
    var values = Array.isArray(binding.groups) ? binding.groups : [];
    var names = Array.isArray(binding.group_names) ? binding.group_names : [];
    var labels = values.map(function (value, index) {
      var id = value && typeof value === "object" ? value.group_openid : value;
      return names[index] || groupLabel(String(id || ""));
    }).filter(Boolean);
    return labels.join("、") || groupLabel(binding.last_violation_group);
  }

  function identitySearchValue(item, extra) {
    var values = [
      item.uid, item.bilibili_uid, item.username, item.member_name, item.identity,
      item.member_openid, item.qq_openid, item.openid, item.union_openid,
      item.group_openid, item.group_name, item.group, groupLabel(item.group_openid),
      item.last_violation_group, item.reason, item.rule, item.category, item.content,
      item.message, item.message_content, item.message_summary, extra
    ];
    if (Array.isArray(item.groups)) {
      values = values.concat(item.groups.map(function (value) {
        var id = value && typeof value === "object" ? value.group_openid : value;
        return String(id || "") + " " + groupLabel(String(id || ""));
      }));
    }
    if (item.members && typeof item.members === "object") {
      values = values.concat(Object.keys(item.members), Object.values(item.members));
    }
    return values.filter(function (value) { return value != null; }).join(" ").toLocaleLowerCase();
  }

  function identityFiltered(items, extra) {
    var query = (element("identity-search").value || "").trim().toLocaleLowerCase();
    return items.filter(function (item) {
      return !query || identitySearchValue(item, extra).includes(query);
    });
  }

  function pageItems(items, kind) {
    var totalPages = Math.max(1, Math.ceil(items.length / identityPageSize));
    identityPages[kind] = Math.min(identityPages[kind], totalPages);
    var start = (identityPages[kind] - 1) * identityPageSize;
    return { items: items.slice(start, start + identityPageSize), totalPages: totalPages };
  }

  function renderPager(id, kind, total, totalPages) {
    var pager = element(id);
    pager.replaceChildren();
    if (!total) return;
    var label = document.createElement("span");
    label.textContent = "第 " + identityPages[kind] + " / " + totalPages + " 页，共 " + total + " 条";
    var previous = document.createElement("button");
    previous.type = "button";
    previous.className = "button secondary small-button";
    previous.textContent = "上一页";
    previous.disabled = identityPages[kind] <= 1;
    previous.addEventListener("click", function () {
      identityPages[kind] -= 1;
      renderIdentities();
    });
    var next = document.createElement("button");
    next.type = "button";
    next.className = "button secondary small-button";
    next.textContent = "下一页";
    next.disabled = identityPages[kind] >= totalPages;
    next.addEventListener("click", function () {
      identityPages[kind] += 1;
      renderIdentities();
    });
    pager.append(label, previous, next);
  }

  function recordField(parent, label, value, code) {
    var item = document.createElement("div");
    item.className = "identity-field";
    var title = document.createElement("span");
    title.className = "identity-field-label";
    title.textContent = label;
    var content = document.createElement(code ? "code" : "span");
    content.textContent = identityText(value);
    item.append(title, content);
    parent.appendChild(item);
  }

  function timestamp(value) {
    if (!value) return "-";
    var number = Number(value);
    var date = new Date(number > 100000000000 ? number : number * 1000);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function renderBindingList(bindings) {
    var list = element("binding-list");
    list.replaceChildren();
    var page = pageItems(bindings, "bindings");
    if (!bindings.length) {
      list.innerHTML = '<p class="empty">暂无匹配的 UID 身份绑定</p>';
      renderPager("binding-pager", "bindings", 0, 1);
      element("binding-count").textContent = "0 条";
      return;
    }
    page.items.forEach(function (binding) {
      var details = document.createElement("details");
      details.className = "identity-card";
      var summary = document.createElement("summary");
      summary.innerHTML = '<strong></strong><span class="identity-card-meta"></span>';
      summary.querySelector("strong").textContent = identityText(binding.username || binding.identity || binding.uid);
      summary.querySelector(".identity-card-meta").textContent =
        "UID " + identityText(binding.uid) + " · " + identityGroups(binding) +
        " · 违规 " + Number(binding.violation_count || 0) + " 次";
      var body = document.createElement("div");
      body.className = "identity-card-body";
      recordField(body, "B站 UID", binding.uid, true);
      recordField(body, "成员 OpenID", binding.member_openid, true);
      recordField(body, "联合 OpenID", binding.union_openid, true);
      recordField(body, "唯一身份键", binding.identity, true);
      recordField(body, "群", identityGroups(binding));
      recordField(body, "最近违规", binding.last_violation_reason || "暂无");
      var actions = document.createElement("div");
      actions.className = "row-actions identity-card-actions";
      actions.appendChild(actionButton("解除绑定", "danger", function (event) {
        event.preventDefault();
        confirmAction("解除 UID 绑定", "确认解除 UID " + binding.uid + " 的唯一身份绑定？", function () {
          apiPost("binding-delete", { uid: String(binding.uid) })
            .then(function () { toast("UID 绑定已解除"); load(); })
            .catch(function (error) { toast("解除失败：" + error.message, true); });
        });
      }));
      body.appendChild(actions);
      details.append(summary, body);
      list.appendChild(details);
    });
    element("binding-count").textContent = bindings.length + " 条";
    renderPager("binding-pager", "bindings", bindings.length, page.totalPages);
  }

  function renderSuspiciousList(suspicious) {
    var list = element("suspicious-list");
    list.replaceChildren();
    var page = pageItems(suspicious, "suspicious");
    if (!suspicious.length) {
      list.innerHTML = '<p class="empty">暂无匹配的待真人验证成员</p>';
      renderPager("suspicious-pager", "suspicious", 0, 1);
      element("suspicious-count").textContent = "0 条";
      return;
    }
    page.items.forEach(function (member) {
      var details = document.createElement("details");
      details.className = "identity-card suspicious-card";
      var summary = document.createElement("summary");
      summary.innerHTML = '<strong></strong><span class="identity-card-meta"></span>';
      summary.querySelector("strong").textContent = identityText(member.username || member.member_openid);
      summary.querySelector(".identity-card-meta").textContent =
        groupLabel(member.group_openid) + " · " + identityText(member.reason);
      var body = document.createElement("div");
      body.className = "identity-card-body";
      recordField(body, "成员 OpenID", member.member_openid, true);
      recordField(body, "群", groupLabel(member.group_openid));
      recordField(body, "标记原因", member.reason);
      recordField(body, "标记时间", timestamp(member.created_at));
      var actions = document.createElement("div");
      actions.className = "row-actions identity-card-actions";
      actions.appendChild(actionButton("解除标记", "danger", function (event) {
        event.preventDefault();
        confirmAction("解除可疑标记", "确认允许该成员恢复正常发言？", function () {
          apiPost("suspicious-clear", {
            group_openid: member.group_openid,
            member_openid: member.member_openid
          }).then(function () { toast("可疑标记已解除"); load(); })
            .catch(function (error) { toast("解除失败：" + error.message, true); });
        });
      }));
      body.appendChild(actions);
      details.append(summary, body);
      list.appendChild(details);
    });
    element("suspicious-count").textContent = suspicious.length + " 条";
    renderPager("suspicious-pager", "suspicious", suspicious.length, page.totalPages);
  }

  function bindingViolationRecords(bindings) {
    var records = [];
    bindings.forEach(function (binding) {
      var nested = Array.isArray(binding.violations)
        ? binding.violations
        : (Array.isArray(binding.violation_records) ? binding.violation_records : []);
      nested.forEach(function (record) {
        records.push(Object.assign({}, record, {
          uid: record.uid || record.bilibili_uid || binding.uid,
          username: record.username || binding.username || binding.identity,
          member_openid: record.member_openid || record.qq_openid || record.openid || binding.member_openid,
          group_openid: record.group_openid || record.last_violation_group,
          _binding: binding
        }));
      });
      if (!nested.length && Number(binding.violation_count || 0) > 0) {
        records.push({
          uid: binding.uid,
          username: binding.username || binding.identity,
          member_openid: binding.member_openid,
          group_openid: binding.last_violation_group,
          reason: binding.last_violation_reason,
          created_at: binding.last_violation_at,
          content: binding.last_violation_content || binding.last_violation_message,
          _summary: true
        });
      }
    });
    return records;
  }

  function renderViolationList(records) {
    var list = element("violation-list");
    list.replaceChildren();
    var page = pageItems(records, "violations");
    if (!records.length) {
      list.innerHTML = '<p class="empty">暂无匹配的违规记录</p>';
      renderPager("violation-pager", "violations", 0, 1);
      element("violation-count").textContent = "0 条";
      return;
    }
    page.items.forEach(function (record) {
      var details = document.createElement("details");
      details.className = "identity-card violation-card";
      var summary = document.createElement("summary");
      summary.innerHTML = '<strong></strong><span class="identity-card-meta"></span>';
      summary.querySelector("strong").textContent = identityText(record.username || record.member_name || record.member_openid || record.uid || record.bilibili_uid);
      summary.querySelector(".identity-card-meta").textContent =
        recordGroupLabel(record) + " · " + identityText(record.reason || record.rule || "内容审查");
      var body = document.createElement("div");
      body.className = "identity-card-body";
      recordField(body, "时间", timestamp(record.created_at || record.occurred_at || record.timestamp || record.at));
      recordField(body, "B站 UID", record.uid || record.bilibili_uid, true);
      recordField(body, "QQ OpenID", record.member_openid || record.qq_openid || record.openid || record.union_openid, true);
      recordField(body, "群", recordGroupLabel(record));
      recordField(body, "命中规则", record.reason || record.rule || record.category);
      recordField(body, "处理动作", record.action === "record_only" ? "仅记录，未撤回" : "已撤回");
      if (record.ai_provider || record.ai_decision || record.ai_reason) {
        recordField(body, "审核模型", record.ai_provider || "-");
        recordField(
          body,
          "AI 判定",
          [record.ai_decision, record.ai_confidence == null ? "" : "置信度 " + record.ai_confidence, record.ai_reason]
            .filter(Boolean).join(" · ") || "-"
        );
      }
      recordField(body, "消息内容", record.content || record.message || record.message_content || record.message_summary ||
        (record._summary ? "当前版本仅保存最近一次违规原因，暂无原始消息内容" : "暂无记录内容"));
      list.appendChild(details);
      details.append(summary, body);
    });
    element("violation-count").textContent = records.length + " 条";
    renderPager("violation-pager", "violations", records.length, page.totalPages);
  }

  function renderIdentities() {
    var sourceBindings = Array.isArray(identities.bindings) ? identities.bindings : [];
    var bindings = identityFiltered(sourceBindings);
    var suspicious = identityFiltered(Array.isArray(identities.suspicious) ? identities.suspicious : []);
    var violations = Array.isArray(identities.violations)
      ? identities.violations
      : (Array.isArray(identities.violation_records) ? identities.violation_records : bindingViolationRecords(sourceBindings));
    violations = identityFiltered(violations);
    renderBindingList(bindings);
    renderSuspiciousList(suspicious);
    renderViolationList(violations);
  }

  function fillOverview(data) {
    ["total", "bound", "active", "pending"].forEach(function (key) {
      element("stat-" + key).textContent = data[key] == null ? "-" : data[key];
    });
  }

  function setView(name) {
    ["groups", "global-keywords", "runtime", "identities"].forEach(function (view) {
      var active = view === name;
      if (!active && view === "global-keywords") closeKeywordRules(element(view + "-view"));
      element(view + "-view").hidden = !active;
      element(view + "-tab").classList.toggle("active", active);
      element(view + "-tab").setAttribute("aria-selected", String(active));
      element(view + "-tab").tabIndex = active ? 0 : -1;
    });
  }

  function constrainKeywordLogic(mode, logic) {
    var all = logic.querySelector('option[value="all"]');
    var exact = mode.value === "exact";
    all.disabled = exact;
    if (exact && logic.value === "all") logic.value = "any";
  }

  function closeKeywordRules(container, keep) {
    if (!container) return;
    container.querySelectorAll("details.keyword-rule-row[open]").forEach(function (rule) {
      if (rule !== keep) rule.removeAttribute("open");
    });
  }

  function bindKeywordRuleDisclosure(row, list, name, mode, enabled, isNew) {
    var summaryName = row.querySelector(".keyword-rule-summary-name");
    var summaryMatch = row.querySelector(".keyword-rule-summary-match");
    var summaryState = row.querySelector(".keyword-rule-summary-state");

    function updateSummary() {
      summaryName.textContent = name.value.trim() || "未命名规则";
      summaryMatch.textContent = mode.value === "exact" ? "完全匹配" : "包含关键词";
      summaryState.textContent = enabled.checked ? "已启用" : "已停用";
      summaryState.classList.toggle("disabled", !enabled.checked);
    }

    name.addEventListener("input", updateSummary);
    mode.addEventListener("change", updateSummary);
    enabled.addEventListener("change", updateSummary);
    row.addEventListener("toggle", function () {
      if (row.open) closeKeywordRules(list, row);
    });
    updateSummary();
    if (isNew) {
      closeKeywordRules(list, row);
      row.open = true;
    }
  }

  function addGlobalKeywordReply(rule) {
    var list = element("global-keyword-replies");
    if (list.querySelectorAll(".global-keyword-row").length >= 100) {
      toast("最多配置 100 条全局关键词回复", true);
      return;
    }
    var empty = list.querySelector(".empty-rules");
    if (empty) empty.remove();
    var isNew = arguments.length === 0;
    var row = document.createElement("details");
    row.className = "global-keyword-row keyword-rule-row";
    row.innerHTML =
      '<summary class="keyword-rule-summary"><strong class="keyword-rule-summary-name"></strong><span class="keyword-rule-summary-meta"><span class="keyword-rule-summary-match"></span><span class="keyword-rule-summary-state"></span></span></summary>' +
      '<div class="keyword-rule-body global-keyword-body">' +
      '<label><span>规则名称</span><input class="global-rule-name" maxlength="80" required></label>' +
      '<label><span>匹配方式</span><select class="global-rule-mode"><option value="contains">包含关键词</option><option value="exact">完全匹配</option></select></label>' +
      '<label class="checkbox global-rule-enabled"><input type="checkbox"><span>启用</span></label>' +
      '<button class="text-button danger global-rule-remove" type="button">删除</button>' +
      '<label class="wide"><span>关键词</span><textarea class="global-rule-keywords" maxlength="2100" rows="3" placeholder="每行一个关键词，最多 20 个" required></textarea></label>' +
      '<label><span>关键词组合</span><select class="global-rule-logic"><option value="any">任一满足（OR）</option><option value="all">全部满足（AND）</option></select></label>' +
      '<label class="wide"><span>回复内容</span><textarea class="global-rule-content" maxlength="1000" rows="3" required></textarea></label>' +
      '</div>';
    rule = rule || {};
    var legacyKeyword = rule.keyword || "";
    row.querySelector(".global-rule-name").value = rule.name || rule.rule_name || legacyKeyword;
    row.querySelector(".global-rule-keywords").value = Array.isArray(rule.keywords)
      ? rule.keywords.join("\n")
      : (rule.keywords || legacyKeyword);
    row.querySelector(".global-rule-content").value = rule.reply || "";
    row.querySelector(".global-rule-mode").value = rule.match_type === "exact" ? "exact" : "contains";
    row.querySelector(".global-rule-logic").value =
      rule.condition_logic === "all" || rule.keyword_logic === "all" || rule.logic === "all"
        ? "all"
        : "any";
    row.querySelector(".global-rule-enabled input").checked = rule.enabled !== false;
    var globalMode = row.querySelector(".global-rule-mode");
    var globalLogic = row.querySelector(".global-rule-logic");
    constrainKeywordLogic(globalMode, globalLogic);
    globalMode.addEventListener("change", function () {
      constrainKeywordLogic(globalMode, globalLogic);
    });

    var targetValues = Array.isArray(rule.group_openids)
      ? rule.group_openids
      : String(rule.group_openids || "").split(/[\s,，;；]+/).filter(Boolean);
    var targets = new Set(targetValues.includes("*") ? [] : targetValues);
    var scope = document.createElement("fieldset");
    scope.className = "global-group-scope";
    var legend = document.createElement("legend");
    legend.textContent = "覆盖群";
    var allLabel = document.createElement("label");
    allLabel.className = "checkbox";
    var allGroups = document.createElement("input");
    allGroups.type = "checkbox";
    allGroups.className = "global-rule-all-groups";
    allGroups.checked = targets.size === 0;
    var allText = document.createElement("span");
    allText.textContent = "全部已绑定群（包含以后新增的群）";
    allLabel.append(allGroups, allText);
    scope.append(legend, allLabel);

    var choices = document.createElement("div");
    choices.className = "group-choice-grid";
    var boundGroups = Array.isArray(globalKeywordConfig.groups) ? globalKeywordConfig.groups : [];
    boundGroups.forEach(function (group) {
      var label = document.createElement("label");
      label.className = "checkbox group-choice";
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "global-rule-group";
      checkbox.value = group.group_openid;
      checkbox.checked = targets.has(group.group_openid);
      checkbox.disabled = allGroups.checked;
      var text = document.createElement("span");
      text.textContent = group.group_name || "待获取群名称";
      text.title = group.group_openid;
      label.append(checkbox, text);
      choices.appendChild(label);
    });
    if (boundGroups.length) scope.appendChild(choices);
    else {
      var noGroups = document.createElement("p");
      noGroups.className = "scope-empty";
      noGroups.textContent = "暂无已绑定群";
      scope.appendChild(noGroups);
    }
    allGroups.addEventListener("change", function () {
      choices.querySelectorAll("input").forEach(function (checkbox) {
        checkbox.disabled = allGroups.checked;
      });
    });
    row.querySelector(".global-rule-remove").addEventListener("click", function () {
      row.remove();
      if (!list.querySelector(".global-keyword-row")) renderGlobalKeywordReplies([]);
    });
    row.querySelector(".global-keyword-body").appendChild(scope);
    list.appendChild(row);
    bindKeywordRuleDisclosure(
      row,
      list,
      row.querySelector(".global-rule-name"),
      globalMode,
      row.querySelector(".global-rule-enabled input"),
      isNew
    );
  }

  function renderGlobalKeywordReplies(rules) {
    var list = element("global-keyword-replies");
    list.replaceChildren();
    if (!Array.isArray(rules) || !rules.length) {
      var empty = document.createElement("p");
      empty.className = "empty-rules";
      empty.textContent = "尚未配置全局关键词回复";
      list.appendChild(empty);
      return;
    }
    rules.forEach(addGlobalKeywordReply);
  }

  function readGlobalKeywordReplies() {
    return Array.from(element("global-keyword-replies").querySelectorAll(".global-keyword-row")).map(function (row) {
      var allGroups = row.querySelector(".global-rule-all-groups").checked;
      var groupOpenids = Array.from(row.querySelectorAll(".global-rule-group:checked")).map(function (checkbox) {
        return checkbox.value;
      });
      if (!allGroups && !groupOpenids.length) throw new Error("请选择至少一个覆盖群，或选择全部已绑定群");
      return {
        name: row.querySelector(".global-rule-name").value,
        keywords: row.querySelector(".global-rule-keywords").value,
        condition_logic: row.querySelector(".global-rule-logic").value,
        reply: row.querySelector(".global-rule-content").value,
        match_type: row.querySelector(".global-rule-mode").value,
        enabled: row.querySelector(".global-rule-enabled input").checked,
        group_openids: allGroups ? [] : groupOpenids
      };
    });
  }

  function saveGlobalKeywordReplies(event) {
    event.preventDefault();
    var rules;
    try {
      rules = readGlobalKeywordReplies();
    } catch (error) {
      toast(error.message, true);
      return;
    }
    var button = element("save-global-keyword-replies");
    button.disabled = true;
    apiPost("global-keyword-replies/save", {
      rules: rules,
      keyword_reply_cooldown_seconds: Number(element("global-keyword-cooldown").value),
      keyword_reply_recall_seconds: Number(element("global-keyword-recall").value)
    })
      .then(function (saved) {
        saved = Array.isArray(saved) ? { rules: saved } : (saved || {});
        globalKeywordConfig = Object.assign({}, globalKeywordConfig, saved);
        element("global-keyword-cooldown").value =
          Number(globalKeywordConfig.keyword_reply_cooldown_seconds || 0);
        element("global-keyword-recall").value =
          Number(globalKeywordConfig.keyword_reply_recall_seconds || 0);
        renderGlobalKeywordReplies(globalKeywordConfig.rules);
        toast("全局关键词回复已保存");
      })
      .catch(function (error) { toast("保存失败：" + error.message, true); })
      .finally(function () { button.disabled = false; });
  }

  function fillProviderSelect(id, emptyLabel, selectedValue) {
    var select = element(id);
    if (!select) return;
    var providers = Array.isArray(runtimeSettings.providers) ? runtimeSettings.providers : [];
    select.replaceChildren();
    var empty = document.createElement("option");
    empty.value = "";
    empty.textContent = emptyLabel;
    select.appendChild(empty);
    providers.forEach(function (provider) {
      if (!provider || !provider.id) return;
      var option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.model || provider.id;
      select.appendChild(option);
    });
    if (selectedValue && !providers.some(function (provider) { return provider.id === selectedValue; })) {
      var missing = document.createElement("option");
      missing.value = selectedValue;
      missing.textContent = "已不可用：" + selectedValue;
      select.appendChild(missing);
    }
    select.value = selectedValue || "";
  }

  function fillProviderMultiSelect(id, selectedValues) {
    var select = element(id);
    if (!select) return;
    var providers = Array.isArray(runtimeSettings.providers) ? runtimeSettings.providers : [];
    var selected = Array.isArray(selectedValues) ? selectedValues : [];
    select.replaceChildren();
    providers.forEach(function (provider) {
      if (!provider || !provider.id) return;
      var option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.model || provider.id;
      option.selected = selected.indexOf(provider.id) >= 0;
      select.appendChild(option);
    });
    selected.forEach(function (providerId) {
      if (!providerId || providers.some(function (provider) { return provider && provider.id === providerId; })) return;
      var missing = document.createElement("option");
      missing.value = providerId;
      missing.textContent = "已不可用：" + providerId;
      missing.selected = true;
      select.appendChild(missing);
    });
  }

  function selectedValues(id) {
    var select = element(id);
    if (!select) return [];
    return Array.from(select.selectedOptions).map(function (option) { return option.value; }).filter(Boolean);
  }

  function fillRuntime(settings) {
    runtimeSettings = settings || {};
    element("runtime-review-interval").value = runtimeSettings.uid_review_interval_seconds || 60;
    element("runtime-settings-command").checked = runtimeSettings.settings_command_enabled !== false;
    element("runtime-panel-recall").checked = runtimeSettings.settings_panel_auto_recall !== false;
    element("runtime-global-reject-keywords").value = runtimeSettings.global_reject_keywords || "";
    element("runtime-message-reject-keywords").value = runtimeSettings.global_message_reject_keywords || "";
    element("runtime-message-reject-reply").value = runtimeSettings.global_message_reject_reply || "";
    element("runtime-message-reject-at").checked = runtimeSettings.global_message_reject_at_member === true;
    element("runtime-member-blacklist").value = runtimeSettings.global_member_blacklist || "";
    element("runtime-member-whitelist").value = runtimeSettings.global_member_whitelist || "";
    element("runtime-blacklist-reply").value = runtimeSettings.global_blacklist_reply || "";
    element("runtime-blacklist-at").checked = runtimeSettings.global_blacklist_at_member === true;
    element("runtime-mute-message").value = runtimeSettings.mute_success_message || "";
    element("runtime-ai-enabled").checked = runtimeSettings.global_ai_review_enabled === true;
    element("runtime-ai-timeout").value = runtimeSettings.global_ai_review_timeout_seconds || 20;
    element("runtime-ai-threshold").value = runtimeSettings.global_ai_review_block_threshold || 95;
    element("runtime-ai-action").value = runtimeSettings.global_ai_review_action || "record_only";
    element("runtime-ai-reject-reply").value = runtimeSettings.global_ai_reject_reply || "";
    element("runtime-ai-reject-at").checked = runtimeSettings.global_ai_reject_at_member === true;
    element("runtime-ai-images").checked = runtimeSettings.global_ai_review_images_enabled === true;
    element("runtime-image-reject-keywords").value = runtimeSettings.global_image_reject_keywords || "";
    element("runtime-image-reject-reply").value = runtimeSettings.global_image_reject_reply || "";
    element("runtime-image-reject-at").checked = runtimeSettings.global_image_reject_at_member === true;
    element("runtime-image-ocr-enabled").checked = runtimeSettings.global_image_ocr_enabled === true;
    element("runtime-image-ocr-timeout").value = runtimeSettings.global_image_ocr_timeout_seconds || 4;
    element("runtime-image-ocr-max-images").value = runtimeSettings.global_image_ocr_max_images || 1;
    element("runtime-live-interval").value = runtimeSettings.bilibili_live_interval_seconds || 60;
    element("runtime-dynamic-interval").value = runtimeSettings.bilibili_dynamic_interval_seconds || 180;
    element("bilibili-login-status").textContent = runtimeSettings.bilibili_logged_in ? "已登录" : "未登录";
    fillProviderSelect(
      "runtime-ai-provider",
      "使用当前会话模型",
      runtimeSettings.global_ai_review_provider_id || ""
    );
    fillProviderMultiSelect(
      "runtime-ai-fallback-providers",
      runtimeSettings.global_ai_review_fallback_provider_ids || []
    );
    fillProviderSelect(
      "runtime-image-ocr-provider",
      "不使用视觉 OCR 模型",
      runtimeSettings.global_image_ocr_provider_id || ""
    );
    element("runtime-ai-provider").disabled = !element("runtime-ai-enabled").checked;
    element("runtime-ai-fallback-providers").disabled = !element("runtime-ai-enabled").checked;
    element("runtime-image-ocr-provider").disabled = !element("runtime-image-ocr-enabled").checked;
  }

  function saveRuntime(event) {
    event.preventDefault();
    var button = element("save-runtime");
    var settings = {
      uid_review_interval_seconds: Number(element("runtime-review-interval").value),
      settings_command_enabled: element("runtime-settings-command").checked,
      settings_panel_auto_recall: element("runtime-panel-recall").checked,
      global_reject_keywords: element("runtime-global-reject-keywords").value,
      global_message_reject_keywords: element("runtime-message-reject-keywords").value,
      global_message_reject_reply: element("runtime-message-reject-reply").value,
      global_message_reject_at_member: element("runtime-message-reject-at").checked,
      global_member_blacklist: element("runtime-member-blacklist").value,
      global_member_whitelist: element("runtime-member-whitelist").value,
      global_blacklist_reply: element("runtime-blacklist-reply").value,
      global_blacklist_at_member: element("runtime-blacklist-at").checked,
      mute_success_message: element("runtime-mute-message").value,
      global_ai_review_enabled: element("runtime-ai-enabled").checked,
      global_ai_review_provider_id: element("runtime-ai-provider").value,
      global_ai_review_fallback_provider_ids: selectedValues("runtime-ai-fallback-providers").slice(0, 3),
      global_ai_review_timeout_seconds: Number(element("runtime-ai-timeout").value),
      global_ai_review_block_threshold: Number(element("runtime-ai-threshold").value),
      global_ai_review_action: element("runtime-ai-action").value,
      global_ai_reject_reply: element("runtime-ai-reject-reply").value,
      global_ai_reject_at_member: element("runtime-ai-reject-at").checked,
      global_ai_review_images_enabled: element("runtime-ai-images").checked,
      global_image_reject_keywords: element("runtime-image-reject-keywords").value,
      global_image_reject_reply: element("runtime-image-reject-reply").value,
      global_image_reject_at_member: element("runtime-image-reject-at").checked,
      global_image_ocr_enabled: element("runtime-image-ocr-enabled").checked,
      global_image_ocr_provider_id: element("runtime-image-ocr-provider").value,
      global_image_ocr_timeout_seconds: Number(element("runtime-image-ocr-timeout").value),
      global_image_ocr_max_images: Number(element("runtime-image-ocr-max-images").value),
      bilibili_live_interval_seconds: Number(element("runtime-live-interval").value),
      bilibili_dynamic_interval_seconds: Number(element("runtime-dynamic-interval").value)
    };
    button.disabled = true;
    apiPost("runtime/save", settings)
      .then(function (saved) {
        fillRuntime(saved);
        toast("全局运行配置已保存");
      })
      .catch(function (error) { toast("保存失败：" + error.message, true); })
      .finally(function () { button.disabled = false; });
  }

  function stopBilibiliLoginPolling() {
    clearTimeout(bilibiliLoginTimer);
    bilibiliLoginTimer = undefined;
  }

  function pollBilibiliLogin() {
    if (!bilibiliLoginKey) return;
    apiPost("bilibili-login/poll", { qrcode_key: bilibiliLoginKey })
      .then(function (result) {
        var status = result && result.status ? result.status : "pending";
        if (status === "success" || status === "confirmed" || (result && result.logged_in === true)) {
          stopBilibiliLoginPolling();
          bilibiliLoginKey = "";
          element("bilibili-login-status").textContent = "已登录";
          element("bilibili-qr-status").textContent = "登录成功";
          toast("B站账号登录成功");
          return;
        }
        if (status === "expired" || status === "cancelled") {
          stopBilibiliLoginPolling();
          bilibiliLoginKey = "";
          element("bilibili-qr-status").textContent = "二维码已失效";
          return;
        }
        element("bilibili-qr-status").textContent = status === "scanned" ? "已扫码，请确认" : "等待扫码";
        bilibiliLoginTimer = setTimeout(pollBilibiliLogin, 2000);
      })
      .catch(function (error) {
        stopBilibiliLoginPolling();
        toast("查询登录状态失败：" + error.message, true);
      });
  }

  function startBilibiliLogin() {
    var button = element("start-bilibili-login");
    button.disabled = true;
    stopBilibiliLoginPolling();
    bilibiliLoginKey = "";
    element("bilibili-qr").hidden = true;
    apiPost("bilibili-login/start", {})
      .then(function (result) {
        bilibiliLoginKey = result && result.qrcode_key ? result.qrcode_key : "";
        var image = result && (result.qr_image || result.qrcode_data_url);
        if (!bilibiliLoginKey || !image) throw new Error("登录服务未返回二维码");
        element("bilibili-qr-image").src = image;
        element("bilibili-qr-status").textContent = "等待扫码";
        element("bilibili-qr").hidden = false;
        pollBilibiliLogin();
      })
      .catch(function (error) { toast("获取二维码失败：" + error.message, true); })
      .finally(function () { button.disabled = false; });
  }

  function load() {
    element("group-rows").innerHTML = '<tr><td class="empty" colspan="6">正在加载...</td></tr>';
    element("batch-toolbar").hidden = true;
    element("select-visible").disabled = true;
    Promise.all([apiGet("overview"), apiGet("list"), apiGet("identities"), apiGet("global-keyword-replies"), apiGet("runtime")])
      .then(function (result) {
        fillOverview(result[0]);
        groups = Array.isArray(result[1]) ? result[1] : [];
        identities = result[2] || { bindings: [], suspicious: [], violations: [] };
        globalKeywordConfig = result[3] || { groups: [], rules: [] };
        element("global-keyword-cooldown").value =
          Number(globalKeywordConfig.keyword_reply_cooldown_seconds || 0);
        element("global-keyword-recall").value =
          Number(globalKeywordConfig.keyword_reply_recall_seconds || 0);
        fillRuntime(result[4]);
        groups.forEach(function (group) {
          if (group.mode === "uid") {
            group.mode = "conditional";
            if (group.uid_check_enabled == null) group.uid_check_enabled = true;
          }
          if (group.uid_exists_auto_approve == null) group.uid_exists_auto_approve = false;
          if (group.reject_keywords == null && group.uid_reject_keywords != null) {
            group.reject_keywords = group.uid_reject_keywords;
          }
        });
        selected.forEach(function (id) {
          if (!groups.some(function (group) { return group.group_openid === id; })) selected.delete(id);
        });
        render();
        renderIdentities();
        renderGlobalKeywordReplies(globalKeywordConfig.rules);
      })
      .catch(function (error) {
        element("group-rows").innerHTML = '<tr><td class="empty error" colspan="6"></td></tr>';
        element("group-rows").querySelector("td").textContent = "加载失败：" + error.message;
        toast("加载失败：" + error.message, true);
      });
  }

  function updateConditionalFields() {
    var mode = element("mode").value;
    element("whitelist-field").hidden = mode !== "native";
    element("scan-field").hidden = mode !== "native";
    document.querySelectorAll(".condition-field").forEach(function (field) {
      field.hidden = mode !== "conditional";
    });
    updateUidDirectField();
  }

  function updateUidDirectField() {
    var direct = element("uid-exists-auto-approve");
    direct.disabled = !element("uid-check-enabled").checked;
    if (direct.disabled) direct.checked = false;
  }

  function addKeywordReply(rule) {
    var list = element("keyword-replies");
    if (list.querySelectorAll(".keyword-reply-row").length >= 100) {
      toast("每群最多配置 100 条关键词回复", true);
      return;
    }
    var empty = list.querySelector(".empty-rules");
    if (empty) empty.remove();
    var isNew = arguments.length === 0;
    var row = document.createElement("details");
    row.className = "keyword-reply-row keyword-rule-row";
    row.innerHTML =
      '<summary class="keyword-rule-summary"><strong class="keyword-rule-summary-name"></strong><span class="keyword-rule-summary-meta"><span class="keyword-rule-summary-match"></span><span class="keyword-rule-summary-state"></span></span></summary>' +
      '<div class="keyword-rule-body keyword-reply-body">' +
      '<label><span>规则名称</span><input class="keyword-reply-name" maxlength="80" required></label>' +
      '<label><span>匹配方式</span><select class="keyword-reply-mode"><option value="contains">包含关键词</option><option value="exact">完全匹配</option></select></label>' +
      '<label class="checkbox keyword-reply-enabled"><input type="checkbox"><span>启用</span></label>' +
      '<button class="text-button danger keyword-reply-remove" type="button">删除</button>' +
      '<label class="wide"><span>关键词</span><textarea class="keyword-reply-keywords" maxlength="2100" rows="3" placeholder="每行一个关键词，最多 20 个" required></textarea></label>' +
      '<label><span>关键词组合</span><select class="keyword-reply-logic"><option value="any">任一满足（OR）</option><option value="all">全部满足（AND）</option></select></label>' +
      '<label class="wide"><span>回复内容</span><textarea class="keyword-reply-content" maxlength="1000" rows="2" required></textarea></label>' +
      '</div>';
    rule = rule || {};
    var legacyKeyword = rule.keyword || "";
    row.querySelector(".keyword-reply-name").value = rule.name || rule.rule_name || legacyKeyword;
    row.querySelector(".keyword-reply-keywords").value = Array.isArray(rule.keywords)
      ? rule.keywords.join("\n")
      : (rule.keywords || legacyKeyword);
    row.querySelector(".keyword-reply-content").value = rule.reply || "";
    row.querySelector(".keyword-reply-mode").value = rule.match_type === "exact" ? "exact" : "contains";
    row.querySelector(".keyword-reply-logic").value =
      rule.condition_logic === "all" || rule.keyword_logic === "all" || rule.logic === "all"
        ? "all"
        : "any";
    row.querySelector(".keyword-reply-enabled input").checked = rule.enabled !== false;
    var groupMode = row.querySelector(".keyword-reply-mode");
    var groupLogic = row.querySelector(".keyword-reply-logic");
    constrainKeywordLogic(groupMode, groupLogic);
    groupMode.addEventListener("change", function () {
      constrainKeywordLogic(groupMode, groupLogic);
    });
    row.querySelector(".keyword-reply-remove").addEventListener("click", function () {
      row.remove();
      if (!list.querySelector(".keyword-reply-row")) renderKeywordReplies([]);
    });
    list.appendChild(row);
    bindKeywordRuleDisclosure(
      row,
      list,
      row.querySelector(".keyword-reply-name"),
      groupMode,
      row.querySelector(".keyword-reply-enabled input"),
      isNew
    );
  }

  function renderKeywordReplies(rules) {
    var list = element("keyword-replies");
    list.replaceChildren();
    if (!Array.isArray(rules) || !rules.length) {
      var empty = document.createElement("p");
      empty.className = "empty-rules";
      empty.textContent = "尚未配置单群关键词回复";
      list.appendChild(empty);
      return;
    }
    rules.forEach(addKeywordReply);
  }

  function readKeywordReplies() {
    return Array.from(element("keyword-replies").querySelectorAll(".keyword-reply-row")).map(function (row) {
      return {
        name: row.querySelector(".keyword-reply-name").value,
        keywords: row.querySelector(".keyword-reply-keywords").value,
        condition_logic: row.querySelector(".keyword-reply-logic").value,
        reply: row.querySelector(".keyword-reply-content").value,
        match_type: row.querySelector(".keyword-reply-mode").value,
        enabled: row.querySelector(".keyword-reply-enabled input").checked
      };
    });
  }

  function openEditor(group) {
    editingGroup = group;
    element("dialog-title").textContent = group.group_name || "编辑群审核";
    element("dialog-subtitle").textContent = group.group_openid;
    element("mode").value = group.mode === "uid" ? "conditional" : (group.mode || "off");
    if (!element("mode").value) element("mode").value = "off";
    element("whitelist").value = group.whitelist_qq_numbers || "";
    element("uid-check-enabled").checked = group.uid_check_enabled === true;
    element("uid-exists-auto-approve").checked =
      group.uid_check_enabled === true && group.uid_exists_auto_approve === true;
    element("approve-keywords").value = group.approve_keywords || "";
    element("reject-keywords").value = group.reject_keywords || group.uid_reject_keywords || "";
    element("condition-logic").value = group.condition_logic || "all";
    if (!element("condition-logic").value) element("condition-logic").value = "all";
    element("fallback-action").value = group.fallback_action || "pending";
    if (!element("fallback-action").value) element("fallback-action").value = "pending";
    element("reject-reason").value = group.button_reject_reason || "管理员拒绝";
    element("scan-pending").checked = group.scan_pending !== false;
    element("fallback-human-verify-enabled").checked = group.fallback_human_verify_enabled === true;
    element("moderation-enabled").checked = group.moderation_enabled === true;
    element("moderation-exempt-admins").checked = group.moderation_exempt_admins !== false;
    element("member-blacklist").value = group.member_blacklist || "";
    element("member-whitelist").value = group.member_whitelist || "";
    element("blacklist-reply").value = group.blacklist_reply || "";
    element("blacklist-at").checked = group.blacklist_at_member === true;
    element("message-reject-keywords").value = group.message_reject_keywords || "";
    element("message-reject-reply").value = group.message_reject_reply || "";
    element("message-reject-at").checked = group.message_reject_at_member === true;
    element("image-keyword-review-enabled").checked = group.image_keyword_review_enabled === true;
    element("image-reject-keywords").value = group.image_reject_keywords || "";
    element("image-reject-reply").value = group.image_reject_reply || "";
    element("image-reject-at").checked = group.image_reject_at_member === true;
    renderKeywordReplies(group.keyword_replies);
    element("image-spam-enabled").checked = group.image_spam_enabled === true;
    element("image-spam-count").value = group.image_spam_count || 5;
    element("image-spam-window").value = group.image_spam_window_seconds || 15;
    element("image-spam-group-min-members").value = group.image_spam_group_min_members || 2;
    element("image-spam-recall-count").value = group.image_spam_recall_count || 5;
    element("image-spam-reply").value = group.image_spam_reply || "";
    element("image-spam-at").checked = group.image_spam_at_member === true;
    element("repeat-review-enabled").checked = group.repeat_review_enabled === true;
    element("repeat-count").value = group.repeat_count || 4;
    element("repeat-window").value = group.repeat_window_seconds || 30;
    element("repeat-mute-min").value = group.repeat_mute_min_seconds || 60;
    element("repeat-mute-max").value = group.repeat_mute_max_seconds || 600;
    element("repeat-reply").value = group.repeat_reply || "";
    element("repeat-at").checked = group.repeat_at_member === true;
    element("bilibili-uids").value = group.bilibili_uids || "";
    element("bilibili-dynamic-enabled").checked = group.bilibili_dynamic_enabled === true;
    element("bilibili-live-enabled").checked = group.bilibili_live_enabled === true;
    updateConditionalFields();
    element("edit-dialog").showModal();
  }

  function save(event) {
    event.preventDefault();
    var button = element("save-button");
    var body = {
      group_openid: editingGroup.group_openid,
      mode: element("mode").value,
      whitelist_qq_numbers: element("whitelist").value,
      uid_check_enabled: element("uid-check-enabled").checked,
      uid_exists_auto_approve: element("uid-exists-auto-approve").checked,
      approve_keywords: element("approve-keywords").value,
      reject_keywords: element("reject-keywords").value,
      condition_logic: element("condition-logic").value,
      fallback_action: element("fallback-action").value,
      button_reject_reason: element("reject-reason").value,
      scan_pending: element("scan-pending").checked,
      fallback_human_verify_enabled: element("fallback-human-verify-enabled").checked,
      moderation_enabled: element("moderation-enabled").checked,
      moderation_exempt_admins: element("moderation-exempt-admins").checked,
      member_blacklist: element("member-blacklist").value,
      member_whitelist: element("member-whitelist").value,
      blacklist_reply: element("blacklist-reply").value,
      blacklist_at_member: element("blacklist-at").checked,
      message_reject_keywords: element("message-reject-keywords").value,
      message_reject_reply: element("message-reject-reply").value,
      message_reject_at_member: element("message-reject-at").checked,
      image_keyword_review_enabled: element("image-keyword-review-enabled").checked,
      image_reject_keywords: element("image-reject-keywords").value,
      image_reject_reply: element("image-reject-reply").value,
      image_reject_at_member: element("image-reject-at").checked,
      keyword_replies: readKeywordReplies(),
      image_spam_enabled: element("image-spam-enabled").checked,
      image_spam_count: Number(element("image-spam-count").value),
      image_spam_window_seconds: Number(element("image-spam-window").value),
      image_spam_group_min_members: Number(element("image-spam-group-min-members").value),
      image_spam_recall_count: Number(element("image-spam-recall-count").value),
      image_spam_reply: element("image-spam-reply").value,
      image_spam_at_member: element("image-spam-at").checked,
      repeat_review_enabled: element("repeat-review-enabled").checked,
      repeat_count: Number(element("repeat-count").value),
      repeat_window_seconds: Number(element("repeat-window").value),
      repeat_mute_min_seconds: Number(element("repeat-mute-min").value),
      repeat_mute_max_seconds: Number(element("repeat-mute-max").value),
      repeat_reply: element("repeat-reply").value,
      repeat_at_member: element("repeat-at").checked,
      bilibili_uids: element("bilibili-uids").value,
      bilibili_dynamic_enabled: element("bilibili-dynamic-enabled").checked,
      bilibili_live_enabled: element("bilibili-live-enabled").checked
    };
    button.disabled = true;
    apiPost("save", body)
      .then(function () {
        element("edit-dialog").close();
        toast("配置已保存");
        load();
      })
      .catch(function (error) { toast("保存失败：" + error.message, true); })
      .finally(function () { button.disabled = false; });
  }

  function openBatchEditor() {
    if (!selected.size) return;
    var form = element("batch-edit-form");
    form.reset();
    form.querySelectorAll("[data-controls]").forEach(function (toggle) {
      element(toggle.dataset.controls).disabled = true;
    });
    element("batch-edit-dialog").showModal();
  }

  function batchChanges() {
    var changes = {};
    [
      ["batch-mode", "mode", false],
      ["batch-uid-check-enabled", "uid_check_enabled", true],
      ["batch-uid-exists-auto-approve", "uid_exists_auto_approve", true],
      ["batch-condition-logic", "condition_logic", false],
      ["batch-fallback-action", "fallback_action", false],
      ["batch-scan-pending", "scan_pending", true],
      ["batch-fallback-human-verify", "fallback_human_verify_enabled", true],
      ["batch-moderation-enabled", "moderation_enabled", true],
      ["batch-moderation-exempt-admins", "moderation_exempt_admins", true],
      ["batch-blacklist-at", "blacklist_at_member", true],
      ["batch-message-at", "message_reject_at_member", true],
      ["batch-image-spam-enabled", "image_spam_enabled", true],
      ["batch-image-keyword-enabled", "image_keyword_review_enabled", true],
      ["batch-image-at", "image_reject_at_member", true],
      ["batch-image-spam-at", "image_spam_at_member", true],
      ["batch-repeat-review-enabled", "repeat_review_enabled", true],
      ["batch-repeat-at", "repeat_at_member", true],
      ["batch-bilibili-dynamic-enabled", "bilibili_dynamic_enabled", true],
      ["batch-bilibili-live-enabled", "bilibili_live_enabled", true]
    ].forEach(function (item) {
      var value = element(item[0]).value;
      if (value !== "") changes[item[1]] = item[2] ? value === "true" : value;
    });
    document.querySelectorAll("[data-controls]").forEach(function (toggle) {
      if (!toggle.checked) return;
      var value = element(toggle.dataset.controls).value;
      changes[toggle.dataset.field] = element(toggle.dataset.controls).type === "number"
        ? Number(value)
        : value;
    });
    return changes;
  }

  function saveBatch(event) {
    event.preventDefault();
    var ids = selectedGroupIds();
    var changes = batchChanges();
    if (!ids.length) {
      element("batch-edit-dialog").close();
      return;
    }
    if (!Object.keys(changes).length) {
      toast("请至少选择一个要覆盖的字段", true);
      return;
    }
    var button = element("batch-save-button");
    button.disabled = true;
    apiPost("batch-save", { group_openids: ids, changes: changes })
      .then(function () {
        element("batch-edit-dialog").close();
        toast("已保存 " + ids.length + " 个群的配置");
        load();
      })
      .catch(function (error) { toast("批量保存失败：" + error.message, true); })
      .finally(function () { button.disabled = false; });
  }

  function confirmAction(title, text, callback) {
    var dialog = element("confirm-dialog");
    element("confirm-title").textContent = title;
    element("confirm-text").textContent = text;
    dialog.returnValue = "";
    dialog.onclose = function () {
      if (dialog.returnValue === "confirm") callback();
    };
    dialog.showModal();
  }

  function syncGroup(group) {
    confirmAction("应用群审核配置", "将当前配置应用到“" + group.group_name + "”？", function () {
      apiPost("sync", { group_openid: group.group_openid })
        .then(function () { toast("配置已应用"); load(); })
        .catch(function (error) { toast("应用失败：" + error.message, true); });
    });
  }

  function syncSelectedGroups() {
    var ids = selectedGroupIds();
    if (!ids.length) return;
    confirmAction("批量应用群审核配置", "将当前配置应用到已选择的 " + ids.length + " 个群？", function () {
      var button = element("batch-sync-button");
      button.disabled = true;
      apiPost("batch-sync", { group_openids: ids })
        .then(function (data) {
          var results = Array.isArray(data) ? data : (data && Array.isArray(data.results) ? data.results : []);
          var failed = results.filter(function (result) { return result.ok === false; }).length;
          if (!failed && data && Number.isInteger(data.failed)) failed = data.failed;
          var firstFailure = results.find(function (result) { return result.ok === false; });
          var failedGroup = firstFailure && groups.find(function (group) {
            return group.group_openid === firstFailure.group_openid;
          });
          var failureDetail = firstFailure
            ? "；首项：“" + (failedGroup ? failedGroup.group_name : firstFailure.group_openid) + "” " + firstFailure.error
            : "";
          toast(
            failed
              ? "批量应用完成：成功 " + (ids.length - failed) + " 个，失败 " + failed + " 个" + failureDetail
              : "已应用 " + ids.length + " 个群的配置",
            failed > 0
          );
          load();
        })
        .catch(function (error) { toast("批量应用失败：" + error.message, true); })
        .finally(function () { button.disabled = false; });
    });
  }

  function removeGroup(group) {
    confirmAction("移除群配置", "确认移除“" + group.group_name + "”？启用中的策略需先关闭并应用。", function () {
      apiPost("delete", { group_openid: group.group_openid })
        .then(function () { toast("群配置已移除"); load(); })
        .catch(function (error) { toast("移除失败：" + error.message, true); });
    });
  }

  element("refresh-button").addEventListener("click", load);
  var viewNames = ["groups", "global-keywords", "runtime", "identities"];
  viewNames.forEach(function (name) {
    element(name + "-tab").addEventListener("click", function () { setView(name); });
  });
  document.querySelector(".view-tabs").addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    var current = viewNames.findIndex(function (name) {
      return element(name + "-tab").getAttribute("aria-selected") === "true";
    });
    var offset = event.key === "ArrowRight" ? 1 : -1;
    var next = (current + offset + viewNames.length) % viewNames.length;
    setView(viewNames[next]);
    element(viewNames[next] + "-tab").focus();
  });
  element("search-input").addEventListener("input", render);
  element("identity-search").addEventListener("input", function () {
    identityPages = { bindings: 1, suspicious: 1, violations: 1 };
    renderIdentities();
  });
  element("identity-page-size").addEventListener("change", function (event) {
    identityPageSize = Math.max(1, Math.min(50, Number(event.target.value) || 10));
    identityPages = { bindings: 1, suspicious: 1, violations: 1 };
    renderIdentities();
  });
  element("select-visible").addEventListener("change", function (event) {
    var limitReached = false;
    visibleGroups().forEach(function (group) {
      if (!event.target.checked) selected.delete(group.group_openid);
      else if (selected.has(group.group_openid)) return;
      else if (selected.size < BATCH_GROUP_LIMIT) selected.add(group.group_openid);
      else limitReached = true;
    });
    if (limitReached) toast("已选择前 " + BATCH_GROUP_LIMIT + " 个群，请分批处理", true);
    render();
  });
  element("clear-selection-button").addEventListener("click", function () {
    selected.clear();
    render();
  });
  element("batch-edit-button").addEventListener("click", openBatchEditor);
  element("batch-sync-button").addEventListener("click", syncSelectedGroups);
  element("mode").addEventListener("change", updateConditionalFields);
  element("uid-check-enabled").addEventListener("change", updateUidDirectField);
  element("add-keyword-reply").addEventListener("click", function () { addKeywordReply(); });
  element("add-global-keyword-reply").addEventListener("click", function () { addGlobalKeywordReply(); });
  element("global-keyword-form").addEventListener("submit", saveGlobalKeywordReplies);
  element("runtime-form").addEventListener("submit", saveRuntime);
  element("runtime-ai-enabled").addEventListener("change", function (event) {
    element("runtime-ai-provider").disabled = !event.target.checked;
    element("runtime-ai-fallback-providers").disabled = !event.target.checked;
  });
  element("runtime-image-ocr-enabled").addEventListener("change", function (event) {
    element("runtime-image-ocr-provider").disabled = !event.target.checked;
  });
  element("start-bilibili-login").addEventListener("click", startBilibiliLogin);
  element("edit-form").addEventListener("submit", save);
  element("batch-edit-form").addEventListener("submit", saveBatch);
  document.addEventListener("invalid", function (event) {
    var rule = event.target.closest && event.target.closest("details.keyword-rule-row");
    if (!rule) return;
    closeKeywordRules(rule.parentElement, rule);
    rule.open = true;
  }, true);
  document.querySelectorAll("[data-controls]").forEach(function (toggle) {
    toggle.addEventListener("change", function () {
      element(toggle.dataset.controls).disabled = !toggle.checked;
    });
  });
  element("close-dialog").addEventListener("click", function () { element("edit-dialog").close(); });
  element("cancel-edit").addEventListener("click", function () { element("edit-dialog").close(); });
  element("close-batch-dialog").addEventListener("click", function () { element("batch-edit-dialog").close(); });
  element("cancel-batch-edit").addEventListener("click", function () { element("batch-edit-dialog").close(); });

  if (bridge && typeof bridge.ready === "function") {
    bridge.ready().then(load).catch(function (error) { toast(error.message, true); });
  } else {
    element("group-rows").innerHTML = '<tr><td class="empty error" colspan="6">插件页面桥接不可用</td></tr>';
  }
})();
