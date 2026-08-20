(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;
  var BATCH_GROUP_LIMIT = 100;
  var groups = [];
  var selected = new Set();
  var identities = { bindings: [], suspicious: [] };
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

  function tableCell(text, code) {
    var cell = document.createElement("td");
    var node = code ? document.createElement("code") : document.createElement("span");
    node.textContent = text == null || text === "" ? "-" : String(text);
    cell.appendChild(node);
    return cell;
  }

  function renderIdentities() {
    var bindingRows = element("binding-rows");
    var suspiciousRows = element("suspicious-rows");
    bindingRows.replaceChildren();
    suspiciousRows.replaceChildren();
    var bindings = Array.isArray(identities.bindings) ? identities.bindings : [];
    var suspicious = Array.isArray(identities.suspicious) ? identities.suspicious : [];
    if (!bindings.length) {
      bindingRows.innerHTML = '<tr><td class="empty" colspan="5">暂无 UID 身份绑定</td></tr>';
    }
    bindings.forEach(function (binding) {
      var row = document.createElement("tr");
      var actions = document.createElement("td");
      actions.className = "row-actions";
      actions.appendChild(actionButton("解除绑定", "danger", function () {
        confirmAction("解除 UID 绑定", "确认解除 UID " + binding.uid + " 的唯一身份绑定？", function () {
          apiPost("binding-delete", { uid: String(binding.uid) })
            .then(function () { toast("UID 绑定已解除"); load(); })
            .catch(function (error) { toast("解除失败：" + error.message, true); });
        });
      }));
      var groupNames = (binding.groups || []).map(function (id) {
        var group = groups.find(function (item) { return item.group_openid === id; });
        return group ? group.group_name : id;
      }).join("、");
      var violation = Number(binding.violation_count || 0) + " 次";
      if (binding.last_violation_reason) violation += "；" + binding.last_violation_reason;
      row.append(
        tableCell(binding.uid, true),
        tableCell(binding.username || binding.identity, false),
        tableCell(groupNames, false),
        tableCell(violation, false),
        actions
      );
      bindingRows.appendChild(row);
    });
    if (!suspicious.length) {
      suspiciousRows.innerHTML = '<tr><td class="empty" colspan="4">暂无待真人验证成员</td></tr>';
    }
    suspicious.forEach(function (member) {
      var row = document.createElement("tr");
      var actions = document.createElement("td");
      actions.className = "row-actions";
      actions.appendChild(actionButton("解除标记", "danger", function () {
        confirmAction("解除可疑标记", "确认允许该成员恢复正常发言？", function () {
          apiPost("suspicious-clear", {
            group_openid: member.group_openid,
            member_openid: member.member_openid
          }).then(function () { toast("可疑标记已解除"); load(); })
            .catch(function (error) { toast("解除失败：" + error.message, true); });
        });
      }));
      row.append(
        tableCell(member.username || member.member_openid, false),
        tableCell(member.group_openid, true),
        tableCell(member.reason, false),
        actions
      );
      suspiciousRows.appendChild(row);
    });
  }

  function fillOverview(data) {
    ["total", "bound", "active", "pending"].forEach(function (key) {
      element("stat-" + key).textContent = data[key] == null ? "-" : data[key];
    });
  }

  function load() {
    element("group-rows").innerHTML = '<tr><td class="empty" colspan="6">正在加载...</td></tr>';
    element("batch-toolbar").hidden = true;
    element("select-visible").disabled = true;
    Promise.all([apiGet("overview"), apiGet("list"), apiGet("identities")])
      .then(function (result) {
        fillOverview(result[0]);
        groups = Array.isArray(result[1]) ? result[1] : [];
        identities = result[2] || { bindings: [], suspicious: [] };
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
    var row = document.createElement("div");
    row.className = "keyword-reply-row";
    row.innerHTML =
      '<label><span>关键词</span><input class="keyword-reply-keyword" maxlength="100" required></label>' +
      '<label><span>匹配方式</span><select class="keyword-reply-mode"><option value="contains">包含关键词</option><option value="exact">完全匹配</option></select></label>' +
      '<label class="checkbox keyword-reply-enabled"><input type="checkbox"><span>启用</span></label>' +
      '<button class="text-button danger keyword-reply-remove" type="button">删除</button>' +
      '<label class="wide"><span>回复内容</span><textarea class="keyword-reply-content" maxlength="1000" rows="2" required></textarea></label>';
    rule = rule || {};
    row.querySelector(".keyword-reply-keyword").value = rule.keyword || "";
    row.querySelector(".keyword-reply-content").value = rule.reply || "";
    row.querySelector(".keyword-reply-mode").value = rule.match_type === "exact" ? "exact" : "contains";
    row.querySelector(".keyword-reply-enabled input").checked = rule.enabled !== false;
    row.querySelector(".keyword-reply-remove").addEventListener("click", function () {
      row.remove();
      if (!list.querySelector(".keyword-reply-row")) renderKeywordReplies([]);
    });
    list.appendChild(row);
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
        keyword: row.querySelector(".keyword-reply-keyword").value,
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
    element("message-reject-keywords").value = group.message_reject_keywords || "";
    renderKeywordReplies(group.keyword_replies);
    element("ai-review-enabled").checked = group.ai_review_enabled === true;
    element("image-spam-enabled").checked = group.image_spam_enabled === true;
    element("image-spam-count").value = group.image_spam_count || 5;
    element("image-spam-window").value = group.image_spam_window_seconds || 15;
    element("repeat-review-enabled").checked = group.repeat_review_enabled === true;
    element("repeat-count").value = group.repeat_count || 4;
    element("repeat-window").value = group.repeat_window_seconds || 30;
    element("repeat-mute-min").value = group.repeat_mute_min_seconds || 60;
    element("repeat-mute-max").value = group.repeat_mute_max_seconds || 600;
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
      message_reject_keywords: element("message-reject-keywords").value,
      keyword_replies: readKeywordReplies(),
      ai_review_enabled: element("ai-review-enabled").checked,
      image_spam_enabled: element("image-spam-enabled").checked,
      image_spam_count: Number(element("image-spam-count").value),
      image_spam_window_seconds: Number(element("image-spam-window").value),
      repeat_review_enabled: element("repeat-review-enabled").checked,
      repeat_count: Number(element("repeat-count").value),
      repeat_window_seconds: Number(element("repeat-window").value),
      repeat_mute_min_seconds: Number(element("repeat-mute-min").value),
      repeat_mute_max_seconds: Number(element("repeat-mute-max").value),
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
      ["batch-ai-review-enabled", "ai_review_enabled", true],
      ["batch-image-spam-enabled", "image_spam_enabled", true],
      ["batch-repeat-review-enabled", "repeat_review_enabled", true],
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
  element("search-input").addEventListener("input", render);
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
  element("edit-form").addEventListener("submit", save);
  element("batch-edit-form").addEventListener("submit", saveBatch);
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
