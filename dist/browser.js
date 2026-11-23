// Browser adapter. Rust owns routes, access rules, calculations, state transitions and the view.
const root = document.getElementById("app");
let route, revision, dragged;
let queue = Promise.resolve();
let needsRefresh = false;
const hash = () => location.hash.slice(1).split("?")[0];
function showError(message) {
    let notice = document.getElementById("connection-status");
    if (!notice) {
        notice = document.createElement("aside");
        notice.id = "connection-status";
        notice.className = "storage-notice";
        notice.setAttribute("role", "alert");
        document.body.prepend(notice);
    }
    const retry = document.createElement("button");
    retry.textContent = "Обновить представление";
    retry.addEventListener("click", () => start());
    notice.replaceChildren(document.createTextNode(message + " "),retry);
}
async function exchange(endpoint, body) {
    const response = await fetch(endpoint, {
        method:"POST", headers:{"Content-Type":"application/json"}, credentials:"same-origin",
        body:JSON.stringify(body), cache:"no-store"
    });
    if (!response.ok) throw new Error(response.status === 409
        ? "Данные изменились в другой вкладке. Обновите представление перед следующей правкой."
        : "Не удалось получить ответ сервера. Проверьте сохранённые данные перед повтором.");
    const result = await response.json();
    if (typeof result.html !== "string" || !Number.isSafeInteger(result.revision)) throw new Error("Сервер вернул неполный ответ.");
    return result;
}
function dispatch(key, value = "", historyMode = "push") {
    const next = async () => {
        if (needsRefresh) return false;
        try {
            const result = await exchange("/api/sandbox/action", {key, value:String(value), revision});
            render(result, key, value, historyMode);
            return true;
        } catch (error) {
            needsRefresh = true;
            showError(error.message);
            return false;
        }
    };
    queue = queue.then(next,next);
    return queue;
}
function render(result, key, value, historyMode) {
    revision = result.revision;
    const active = document.activeElement;
    const samePage = route === result.route && key !== "session";
    const open = samePage ? [...root.querySelectorAll("details[open]")].map(d => d.querySelector("summary")?.textContent) : [];
    const focusKey = samePage && active?.matches("input[data-act],select[data-act],textarea[data-act]") ? active.dataset.act : null;
    const cursor = focusKey && active.tagName === "INPUT" ? active.selectionStart : null;
    route = result.route;
    root.innerHTML = result.html;
    root.dataset.route = result.route;
    root.dataset.revision = String(result.revision);
    root.removeAttribute("aria-busy");
    document.documentElement.dataset.theme = result.theme;
    document.title = result.title + " · Транстехком";
    if (route !== hash()) history[historyMode === "replace" ? "replaceState" : "pushState"](null, "", "#" + route);
    if (!samePage) window.scrollTo({top: 0, behavior: "instant"});
    if (open.length) root.querySelectorAll("details").forEach(d => { if (open.includes(d.querySelector("summary")?.textContent)) d.open = true; });
    if (focusKey) {
        const el = [...root.querySelectorAll("[data-act]")].find(e => e.dataset.act === focusKey && e.tagName === active.tagName);
        el?.focus({preventScroll:true});
        if (cursor !== null && el?.setSelectionRange && ["search","text"].includes(el.type)) el.setSelectionRange(cursor, cursor);
    }
    if ((key === "dialog" && value) || key === "help-save") root.querySelector("[role=dialog] textarea, [role=dialog] button")?.focus();
    if (key === "dialog" && !value) root.querySelector(".head-tools [data-act=dialog]")?.focus({preventScroll:true});
}
root.addEventListener("click", async event => {
    const split = event.target.closest("[data-split]");
    if (split) {
        const tile = split.closest(".split");
        const expanded = !tile.classList.contains("expanded");
        root.querySelectorAll(".split.expanded").forEach(t => t.classList.remove("expanded"));
        tile.classList.toggle("expanded", expanded);
        split.setAttribute("aria-expanded", String(expanded));
        if(expanded) tile.querySelector(".tile-choices a")?.focus();
        return;
    }
    const el = event.target.closest("[data-act]");
    if (!el || el.matches("input,select,textarea,form") || el.disabled) return;
    if (el.matches("a") && (event.ctrlKey || event.metaKey || event.shiftKey)) return;
    event.preventDefault();
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    const dest = el.dataset.scroll;
    const ok = await dispatch(el.dataset.act, el.dataset.val ?? el.value ?? "");
    if (ok && dest) document.getElementById(dest)?.scrollIntoView({block:"start", behavior:"smooth"});
});
root.addEventListener("change", event => {
    const el = event.target;
    if (!el.matches("input[data-act], select[data-act], textarea[data-act]")) return;
    dispatch(el.dataset.act, el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value);
});
root.addEventListener("submit", event => {
    const form = event.target;
    if (!form.dataset.act) return;
    event.preventDefault();
    if (form.dataset.act === "help-save") {
        submitHelp(form);
        return;
    }
    dispatch(form.dataset.act, (form.querySelector("[data-value-field]") ?? form.elements.namedItem("value"))?.value ?? "");
});

// The clipboard and file input expose browser File objects. The Rust side owns
// validation, addressing, storage and rendering of the resulting submission.
const pendingHelpFiles = new WeakMap();
function addHelpFiles(form, list) {
    const files = pendingHelpFiles.get(form) ?? [];
    const status = form.querySelector("[data-help-status]");
    status.textContent = "";
    for (const file of list) {
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 12_000_000) {
            status.textContent = "Выберите PNG, JPEG или WebP до 12 МБ.";
            continue;
        }
        if (files.length >= 3) {
            status.textContent = "Уже прикреплены три изображения.";
            continue;
        }
        files.push(file);
    }
    pendingHelpFiles.set(form, files);
    const items = form.querySelector("[data-help-list]");
    items.replaceChildren();
    for (const [i,file] of files.entries()) {
        const item = document.createElement("li");
        item.append(document.createTextNode(file.name + " "));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Убрать";
        remove.addEventListener("click", () => { files.splice(i,1); addHelpFiles(form, []); });
        item.append(remove);
        items.append(item);
    }
}
root.addEventListener("change", event => {
    if (event.target.matches("[data-help-files]")) {
        addHelpFiles(event.target.form, event.target.files);
        event.target.value = "";
    }
});
root.addEventListener("paste", event => {
    const form = root.querySelector('form[data-act="help-save"]');
    const files = Array.from(event.clipboardData?.files ?? []);
    if (form && files.length) {
        event.preventDefault();
        addHelpFiles(form, files);
    }
});
async function helpImage(file) {
    const url = URL.createObjectURL(file);
    try {
        const img = new Image();
        img.src = url;
        await img.decode();
        const canvas = document.createElement("canvas");
        let scale = Math.min(1, 1800 / Math.max(img.width, img.height));
        const context = canvas.getContext("2d");
        for (let i = 0; i < 5; i++) {
            canvas.width = Math.max(1, Math.floor(img.width * scale));
            canvas.height = Math.max(1, Math.floor(img.height * scale));
            context.fillStyle = "#ffffff";
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.drawImage(img, 0, 0, canvas.width, canvas.height);
            const data = canvas.toDataURL("image/jpeg", .87);
            if (data.length <= 500_000) return {name:file.name.slice(0,160) || "Скриншот", url:data};
            scale *= .72;
        }
        throw new Error("image size");
    } finally { URL.revokeObjectURL(url); }
}
async function submitHelp(form) {
    const submit = form.querySelector("[type=submit]");
    if (submit.disabled) return;
    submit.disabled = true;
    const status = form.querySelector("[data-help-status]");
    status.textContent = "Сохраняем обращение…";
    try {
        const images = await Promise.all((pendingHelpFiles.get(form) ?? []).map(helpImage));
        if (form.isConnected) await dispatch("help-save", JSON.stringify({text:form.querySelector("textarea").value, images}));
    } catch {
        status.textContent = "Не удалось прочитать изображение. Уберите его или выберите другой файл.";
    } finally { submit.disabled = false; }
}
document.addEventListener("keydown", event => {
    const dialog = root.querySelector("[role=dialog]");
    if (dialog && event.key === "Tab") {
        const controls = [...dialog.querySelectorAll("button,textarea,input,a[href],summary,select")].filter(el => !el.disabled && el.getClientRects().length);
        const edge = event.shiftKey ? controls[0] : controls[controls.length - 1];
        if (controls.length && (document.activeElement === edge || !dialog.contains(document.activeElement))) {
            event.preventDefault();
            (event.shiftKey ? controls[controls.length - 1] : controls[0]).focus();
        }
    }
    if (event.key === "Escape") {
        if(root.querySelector("[role=dialog]")) dispatch("dialog");
        root.querySelectorAll(".split.expanded").forEach(t => t.classList.remove("expanded"));
    }
    if((event.key===" " || event.key==="Enter") && event.target.matches("svg [role=button]")) {
        event.preventDefault(); dispatch(event.target.dataset.act, event.target.dataset.val);
    }
});
root.addEventListener("dragstart", e => { dragged = e.target.closest("[data-drag-key]")?.dataset.dragKey; if(dragged) e.dataTransfer.setData("text/plain",dragged); });
root.addEventListener("dragover", e => { if(dragged && e.target.closest("[data-drop-key]")) e.preventDefault(); });
root.addEventListener("drop", e => { const dest=e.target.closest("[data-drop-key]"); if(dragged && dest){e.preventDefault();dispatch("misc-access-move",dragged+"|"+dest.dataset.dropKey);} dragged=null; });
root.addEventListener("dragend", () => { dragged=null; });
window.addEventListener("hashchange", () => {if(revision && hash() !== route) dispatch("nav",hash() || "home","replace");});
async function start() {
    const button = document.querySelector("#connection-status button");
    if (button) button.disabled = true;
    try {
        const result = await exchange("/api/sandbox/session",{route:hash() || "home",time:new Date().toISOString()});
        needsRefresh = false;
        render(result,"init","","replace");
        document.getElementById("connection-status")?.remove();
    } catch (error) {
        needsRefresh = true;
        root.removeAttribute("aria-busy");
        showError(error.message);
    }
}
await start();

