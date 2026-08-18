(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;
  var groups = [];
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

  function render() {
    var query = element("search-input").value.trim().toLocaleLowerCase();
    var visible = groups.filter(function (group) {
      return !query || (group.group_name + " " + group.group_openid).toLocaleLowerCase().includes(query);
    });
    var tbody = element("group-rows");
    tbody.replaceChildren();

    if (!visible.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = document.createElement("td");
      emptyCell.colSpan = 5;
      emptyCell.className = "empty";
      emptyCell.textContent = groups.length ? "没有匹配的群" : "暂无群配置，请先在群内使用 /审核设置 绑定";
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
      return;
    }

    visible.forEach(function (group) {
      var row = document.createElement("tr");
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
      row.append(identity, mode, bound, sync, actions);
      tbody.appendChild(row);
    });
  }

  function fillOverview(data) {
    ["total", "bound", "active", "pending"].forEach(function (key) {
      element("stat-" + key).textContent = data[key] == null ? "-" : data[key];
    });
  }

  function load() {
    element("group-rows").innerHTML = '<tr><td class="empty" colspan="5">正在加载...</td></tr>';
    Promise.all([apiGet("overview"), apiGet("list")])
      .then(function (result) {
        fillOverview(result[0]);
        groups = Array.isArray(result[1]) ? result[1] : [];
        groups.forEach(function (group) {
          if (group.mode === "uid") {
            group.mode = "conditional";
            if (group.uid_check_enabled == null) group.uid_check_enabled = true;
          }
          if (group.reject_keywords == null && group.uid_reject_keywords != null) {
            group.reject_keywords = group.uid_reject_keywords;
          }
        });
        render();
      })
      .catch(function (error) {
        element("group-rows").innerHTML = '<tr><td class="empty error" colspan="5"></td></tr>';
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
  }

  function openEditor(group) {
    editingGroup = group;
    element("dialog-title").textContent = group.group_name || "编辑群审核";
    element("dialog-subtitle").textContent = group.group_openid;
    element("mode").value = group.mode === "uid" ? "conditional" : (group.mode || "off");
    if (!element("mode").value) element("mode").value = "off";
    element("whitelist").value = group.whitelist_qq_numbers || "";
    element("uid-check-enabled").checked = group.uid_check_enabled === true;
    element("approve-keywords").value = group.approve_keywords || "";
    element("reject-keywords").value = group.reject_keywords || group.uid_reject_keywords || "";
    element("condition-logic").value = group.condition_logic || "all";
    if (!element("condition-logic").value) element("condition-logic").value = "all";
    element("fallback-action").value = group.fallback_action || "pending";
    if (!element("fallback-action").value) element("fallback-action").value = "pending";
    element("reject-reason").value = group.button_reject_reason || "管理员拒绝";
    element("scan-pending").checked = group.scan_pending !== false;
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
      approve_keywords: element("approve-keywords").value,
      reject_keywords: element("reject-keywords").value,
      condition_logic: element("condition-logic").value,
      fallback_action: element("fallback-action").value,
      button_reject_reason: element("reject-reason").value,
      scan_pending: element("scan-pending").checked
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

  function removeGroup(group) {
    confirmAction("移除群配置", "确认移除“" + group.group_name + "”？启用中的策略需先关闭并应用。", function () {
      apiPost("delete", { group_openid: group.group_openid })
        .then(function () { toast("群配置已移除"); load(); })
        .catch(function (error) { toast("移除失败：" + error.message, true); });
    });
  }

  element("refresh-button").addEventListener("click", load);
  element("search-input").addEventListener("input", render);
  element("mode").addEventListener("change", updateConditionalFields);
  element("edit-form").addEventListener("submit", save);
  element("close-dialog").addEventListener("click", function () { element("edit-dialog").close(); });
  element("cancel-edit").addEventListener("click", function () { element("edit-dialog").close(); });

  if (bridge && typeof bridge.ready === "function") {
    bridge.ready().then(load).catch(function (error) { toast(error.message, true); });
  } else {
    element("group-rows").innerHTML = '<tr><td class="empty error" colspan="5">插件页面桥接不可用</td></tr>';
  }
})();
