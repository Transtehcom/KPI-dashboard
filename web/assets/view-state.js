/* Keep editing and scrolling stable while a report refreshes. */
(() => {
  "use strict";
  const temporal = 'input[type=date],input[type=month],input[type=datetime-local],input[type=number]';
  const editing = 'input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=hidden]),textarea,[contenteditable=true]';
  const pendingChanges = new WeakSet(), writes = new Map();
  let saving = 0;

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!input.matches?.(temporal)) return;
    if (!event.uiCommitted && document.activeElement === input) {
      pendingChanges.add(input);
      event.stopImmediatePropagation();
      return;
    }
    pendingChanges.delete(input);
    if (!input.validity.valid) event.stopImmediatePropagation();
  }, true);
  document.addEventListener("focusout", (event) => {
    const input = event.target;
    if (!pendingChanges.has(input)) return;
    queueMicrotask(() => {
      if (!pendingChanges.has(input) || !input.isConnected || !input.validity.valid) return;
      pendingChanges.delete(input);
      const change = new Event("change", {bubbles:true});
      change.uiCommitted = true;
      input.dispatchEvent(change);
    });
  }, true);

  async function save(operation, key) {
    saving++;
    const task = (writes.get(key) || Promise.resolve()).catch(() => {}).then(operation);
    writes.set(key, task);
    try { return await task; }
    finally {
      saving--;
      if (writes.get(key) === task) writes.delete(key);
    }
  }

  function whenIdle(render, current = () => true) {
    return new Promise((resolve, reject) => {
      const check = () => {
        if (!current()) { resolve(); return; }
        if (busy()) {
          setTimeout(check, 120);
          return;
        }
        try { resolve(render()); } catch (error) { reject(error); }
      };
      check();
    });
  }

  function busy() {
    return !!(saving || document.querySelector('dialog[open],.msel[open]') || document.activeElement?.matches(editing));
  }

  function address(root, element) {
    const path = [];
    while (element && element !== root) {
      if (element.id) return {id:element.id, path};
      if (element.hasAttribute("data-f")) {
        const row = element.closest("tr[data-id],tr[data-iid],tr[data-pid],tr[data-rid]");
        const attribute = ["data-id","data-iid","data-pid","data-rid"].find((name) => row?.hasAttribute(name));
        if (attribute) return {field:{attribute,id:row.getAttribute(attribute),scope:row.closest("table")?.dataset.path || "",name:element.dataset.f},path};
      }
      if (element.matches('[data-entry][data-ref]')) {
        return {entry:[element.dataset.table,element.dataset.ref,element.dataset.field],path};
      }
      const referenceType = ["data-client-approval","data-lead-approval","data-payment-mode"].find((name) => element.hasAttribute(name));
      if (referenceType) return {reference:[referenceType,element.dataset.ref],path};
      for (const name of ["data-pick","data-id","data-iid","data-pid","data-rid"]) {
        if (element.hasAttribute(name)) return {record:[name,element.getAttribute(name)],scope:element.closest("table")?.dataset.path || "",path};
      }
      const parent = element.parentElement;
      if (!parent) return null;
      path.unshift([...parent.children].indexOf(element));
      element = parent;
    }
    return element === root ? {path} : null;
  }
  function locate(root, key) {
    if (!key) return null;
    let element = root;
    if (key.id) element = document.getElementById(key.id);
    if (key.entry) element = [...root.querySelectorAll('[data-entry][data-ref]')].find((input) =>
      [input.dataset.table,input.dataset.ref,input.dataset.field].every((value,index) => value === key.entry[index]));
    if (key.reference) element = [...root.querySelectorAll(`[${key.reference[0]}]`)].find((input) => input.dataset.ref === key.reference[1]);
    if (key.record) element = [...root.querySelectorAll(`[${key.record[0]}]`)].find((node) => node.getAttribute(key.record[0]) === key.record[1] && (node.closest("table")?.dataset.path || "") === key.scope);
    if (key.field) {
      const row = [...root.querySelectorAll(`[${key.field.attribute}]`)].find((node) => node.getAttribute(key.field.attribute) === key.field.id && (node.closest("table")?.dataset.path || "") === key.field.scope);
      element = [...(row?.querySelectorAll("[data-f]") || [])].find((input) => input.dataset.f === key.field.name);
    }
    if (!element || !root.contains(element)) return null;
    for (const index of key.path) element = element?.children[index];
    return element || null;
  }
  function dirty(input) {
    if (input.type === "file") return false;
    if (input.closest(".msel") || input.hasAttribute("data-pick") || input.id === "cPickAll") return false;
    if (input.type === "checkbox" || input.type === "radio") return input.checked !== input.defaultChecked;
    if (input.tagName === "SELECT") {
      const options = [...input.options], hasDefault = options.some((option) => option.defaultSelected);
      return options.some((option,index) => option.selected !== (hasDefault ? option.defaultSelected : !input.multiple && index === 0));
    }
    return input.value !== input.defaultValue;
  }
  function saved(input, value, checked) {
    if (input.tagName === "SELECT") {
      for (const option of input.options) option.defaultSelected = option.value === value;
    } else if (input.type === "checkbox" || input.type === "radio") input.defaultChecked = checked;
    else input.defaultValue = value;
  }
  function keepView(root, view) {
    if (root && root.dataset.uiView !== view) { root.dataset.uiView = view; return () => {}; }
    if (!root?.firstElementChild || root.firstElementChild.classList.contains("empty")) return () => {};
    const focused = root.contains(document.activeElement) ? document.activeElement : null;
    const focus = address(root, focused), selection = focused?.selectionStart == null ? null : [focused.selectionStart,focused.selectionEnd];
    const drafts = [...root.querySelectorAll("input,textarea,select")].filter(dirty).map((input) => ({
      key:address(root,input),value:input.value,checked:input.checked,
      selected:input.tagName === "SELECT" ? [...input.selectedOptions].map((option) => option.value) : null
    }));
    const disclosures = [...root.querySelectorAll("details")].map((element) => [address(root,element),element.open]);
    const scrolls = [...root.querySelectorAll("*")].filter((element) => element.scrollLeft || element.scrollTop)
      .map((element) => [address(root,element),element.scrollLeft,element.scrollTop]);
    const top = window.scrollY, left = window.scrollX;
    const anchor = focused || [...root.querySelectorAll("[data-kpi-code],.panel,.head")].find((element) => element.getBoundingClientRect().bottom > 100);
    const anchorKey = address(root,anchor), anchorTop = anchor?.getBoundingClientRect().top;
    const overflowAnchor = root.style.overflowAnchor;
    root.style.overflowAnchor = "none";
    root.dataset.uiRefresh = "";
    return () => {
      for (const [key,open] of disclosures) { const element = locate(root,key); if (element?.tagName === "DETAILS") element.open = open; }
      for (const draft of drafts) {
        const input = locate(root,draft.key);
        if (!input?.matches("input,textarea,select") || input.type === "file") continue;
        if (draft.selected) for (const option of input.options) option.selected = draft.selected.includes(option.value);
        else if (input.type === "checkbox" || input.type === "radio") input.checked = draft.checked;
        else input.value = draft.value;
      }
      for (const [key,x,y] of scrolls) { const element = locate(root,key); if (element) { element.scrollLeft = x; element.scrollTop = y; } }
      const nextFocus = locate(root,focus);
      if (nextFocus && !nextFocus.disabled) {
        nextFocus.focus({preventScroll:true});
        if (selection && nextFocus.setSelectionRange) nextFocus.setSelectionRange(...selection);
      }
      const nextAnchor = locate(root,anchorKey);
      window.scrollTo({left,top:Math.max(0,top + (nextAnchor ? nextAnchor.getBoundingClientRect().top - anchorTop : 0)),behavior:"instant"});
      requestAnimationFrame(() => requestAnimationFrame(() => { delete root.dataset.uiRefresh; root.style.overflowAnchor = overflowAnchor; }));
    };
  }
  globalThis.DashboardUI = {keepView,whenIdle,save,busy,saved};
})();
