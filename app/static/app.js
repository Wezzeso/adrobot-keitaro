const byId = (id) => document.getElementById(id);
const createForm = byId("create-campaign");
const createFields = byId("create-fields");
const createStatus = byId("create-status");
const campaignPicker = byId("campaign-picker");
const refreshFlows = byId("refresh-flows");
const campaignStatus = byId("campaign-status");
const flowControls = byId("flow-controls");
const flowPicker = byId("flow-picker");
const flowPlaceholder = byId("flow-placeholder");
const offersForm = byId("offers-form");
const offersTable = byId("offers-table");
const offerRows = byId("offer-rows");
const offerSearch = byId("offer-search");
const availableOffer = byId("available-offer");
const addOffer = byId("add-offer");
const resetOffers = byId("reset-offers");
const saveOffers = byId("save-offers");
const offersStatus = byId("offers-status");
const catalogFields = {
  offers: byId("initial-offer"),
  domains: byId("domain"),
  groups: byId("group"),
  sources: byId("source"),
};

let catalog = null;
let campaign = null;
let flow = null;
let draft = [];
let busy = false;

async function request(path, method = "GET", body) {
  const options = { method, headers: { Accept: "application/json" }, cache: "no-store" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Не удалось подключиться к серверу.");
  }
  const result = await response.json().catch(() => null);
  if (response.ok && result !== null) {
    return result;
  }
  if (typeof result?.detail === "string") {
    throw new Error(result.detail);
  }
  if (Array.isArray(result?.detail) && result.detail[0]?.msg) {
    throw new Error(result.detail[0].msg.replace(/^Value error, /, ""));
  }
  throw new Error("Не удалось выполнить запрос. Проверьте данные и попробуйте ещё раз.");
}

function message(element, text, kind = "info") {
  element.textContent = text;
  element.dataset.kind = kind;
}

function setOptions(select, items, placeholder, selected = null) {
  select.replaceChildren(new Option(placeholder, ""));
  for (const item of items) {
    select.add(new Option(item.name, String(item.id)));
  }
  select.value = selected === null ? "" : String(selected);
}

function cloneOffers(offers) {
  return offers.map((offer) => ({ ...offer }));
}

function isDirty() {
  return Boolean(flow?.editable) && JSON.stringify(flow.offers) !== JSON.stringify(draft);
}

function mayDiscard() {
  return !isDirty() || window.confirm("Отменить неопубликованные изменения?");
}

function recalculateShares(offers) {
  const active = offers.filter((offer) => !offer.archived);
  if (active.length === 0) {
    throw new Error("Оставьте хотя бы один оффер в потоке.");
  }
  const open = active.filter((offer) => !offer.pinned);
  const remaining = 100 - active.reduce((sum, offer) => sum + (offer.pinned ? offer.weight : 0), 0);
  if (remaining < open.length || (!open.length && remaining !== 0)) {
    throw new Error("Открепите долю одного из офферов, чтобы пересчитать веса.");
  }
  if (open.length === 0) {
    return;
  }
  const common = Math.floor(remaining / open.length);
  const remainder = remaining % open.length;
  open.forEach((offer, index) => {
    offer.weight = common + (index >= open.length - remainder ? 1 : 0);
  });
}

function updateControls() {
  createFields.disabled = busy || !catalog || Object.keys(catalogFields).some((key) => !catalog[key].length);
  campaignPicker.disabled = busy || !catalog?.campaigns.length;
  refreshFlows.disabled = busy || !campaignPicker.value;
  flowPicker.disabled = busy || !campaign?.flows.length;
  offerSearch.disabled = busy || !flow?.editable;
  availableOffer.disabled = offerSearch.disabled || availableOffer.options.length < 2;
  addOffer.disabled = availableOffer.disabled || !availableOffer.value;

  const active = draft.filter((offer) => !offer.archived);
  const total = active.reduce((sum, offer) => sum + offer.weight, 0);
  const dirty = isDirty();
  resetOffers.disabled = busy || !dirty;
  saveOffers.disabled = busy || !dirty || active.length === 0 || total !== 100;
  byId("weight-total").textContent = "Суммарная доля " + total + "%";
  const status = byId("draft-status");
  status.textContent = dirty ? "Черновик" : "Опубликовано";
  status.dataset.kind = dirty ? "draft" : "published";

  for (const button of offerRows.querySelectorAll("button")) {
    button.disabled = busy || button.dataset.allowed === "false";
  }
}

async function perform(action, status) {
  if (busy) {
    return;
  }
  busy = true;
  updateControls();
  try {
    await action();
  } catch (error) {
    message(status, error.message, "error");
  } finally {
    busy = false;
    updateControls();
  }
}

function updateSearch() {
  const term = offerSearch.value.trim().toLocaleLowerCase("ru");
  const used = new Set(draft.map((offer) => offer.id));
  const choices = (catalog?.offers ?? []).filter(({ id, name }) => {
    return !used.has(id) && (String(id).includes(term) || name.toLocaleLowerCase("ru").includes(term));
  });
  const selected = Number(availableOffer.value) || null;
  setOptions(availableOffer, choices, choices.length ? "Выберите оффер" : "Нет подходящих офферов", selected);
  updateControls();
}

function actionButton(text, action, offer, allowed) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "offer-action";
  button.textContent = text;
  button.dataset.action = action;
  button.dataset.id = String(offer.id);
  button.dataset.allowed = String(allowed);
  button.setAttribute("aria-label", text + " · " + offer.name);
  return button;
}

function renderOffers() {
  const available = new Set((catalog?.offers ?? []).map((offer) => offer.id));
  const activeCount = draft.filter((offer) => !offer.archived).length;
  offerRows.replaceChildren();
  for (const offer of draft) {
    const row = offerRows.insertRow();
    row.dataset.archived = String(offer.archived);
    const label = row.insertCell();
    label.textContent = offer.name;
    const meta = document.createElement("small");
    meta.textContent = "ID " + offer.id + (offer.archived ? " · В архиве" : "");
    label.append(meta);

    const share = row.insertCell();
    share.className = "share";
    share.textContent = offer.weight + "%";

    const actions = row.insertCell();
    actions.className = "offer-actions";
    if (offer.archived) {
      actions.append(actionButton("Вернуть", "restore", offer, available.has(offer.id)));
    } else {
      const pin = actionButton(offer.pinned ? "Открепить" : "Закрепить", "pin", offer, true);
      pin.setAttribute("aria-pressed", String(offer.pinned));
      actions.append(pin, actionButton("Удалить", "remove", offer, activeCount > 1));
    }
  }
  offersTable.hidden = draft.length === 0;
  byId("empty-offers").hidden = !offersTable.hidden;
  updateSearch();
}

function showFlow(next) {
  flow = next ?? null;
  draft = cloneOffers(flow?.offers ?? []);
  flowPicker.value = flow ? String(flow.id) : "";
  offersForm.hidden = !flow?.editable;
  flowPlaceholder.hidden = !flow || flow.editable;
  offerSearch.value = "";
  message(offersStatus, "");
  renderOffers();
}

async function loadCampaign(id) {
  if (!id) {
    campaign = null;
    flowControls.hidden = true;
    message(campaignStatus, "Создайте кампанию, чтобы начать работу с потоками.");
    showFlow(null);
    return;
  }
  message(campaignStatus, "Загружаем потоки…");
  try {
    const next = await request("/api/campaigns/" + id + "/editor");
    const previousFlow = campaign?.id === id ? flow?.id : null;
    campaign = next;
    const preferred = next.flows.find((item) => item.id === previousFlow);
    const selected = preferred ?? next.flows.find((item) => item.editable) ?? next.flows[0];
    setOptions(flowPicker, next.flows, "Выберите поток", selected?.id ?? null);
    flowControls.hidden = next.flows.length === 0;
    message(campaignStatus, next.flows.length ? "" : "В кампании нет потоков.");
    showFlow(selected);
  } catch (error) {
    campaignPicker.value = String(campaign?.id ?? id);
    throw error;
  }
}

async function loadCatalog(preferred) {
  const next = await request("/api/catalog");
  catalog = next;
  const mode = byId("tracker-mode");
  mode.textContent = next.mode === "keitaro" ? "Keitaro" : "Демо · SQLite";
  mode.hidden = false;
  byId("catalog-notice").hidden = true;
  for (const [key, select] of Object.entries(catalogFields)) {
    setOptions(select, catalog[key], "Выберите", Number(select.value) || null);
  }
  const selected = catalog.campaigns.find((item) => item.id === (preferred ?? campaign?.id))
    ?? catalog.campaigns[0];
  setOptions(campaignPicker, catalog.campaigns, "Выберите кампанию", selected?.id ?? null);
  await loadCampaign(selected?.id);
}

function changeOffers(change) {
  const next = cloneOffers(draft);
  try {
    change(next);
    recalculateShares(next);
    draft = next;
    message(offersStatus, "");
    renderOffers();
  } catch (error) {
    message(offersStatus, error.message, "error");
  }
}

createForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!mayDiscard()) {
    return;
  }
  const fields = new FormData(createForm);
  const name = fields.get("name")?.trim();
  if (!name) {
    message(createStatus, "Введите название кампании.", "error");
    createForm.elements.name.focus();
    return;
  }
  perform(async () => {
    message(createStatus, "Создаём кампанию…");
    const created = await request("/api/campaigns", "POST", {
      name,
      country: fields.get("country").toUpperCase(),
      offer_id: Number(fields.get("offer_id")),
      domain_id: Number(fields.get("domain_id")),
      group_id: Number(fields.get("group_id")),
      source_id: Number(fields.get("source_id")),
    });
    createForm.reset();
    message(createStatus, "Кампания создана.", "success");
    try {
      await loadCatalog(created.id);
    } catch (error) {
      message(campaignStatus, error.message, "error");
    }
  }, createStatus);
});

campaignPicker.addEventListener("change", () => {
  if (!mayDiscard()) {
    campaignPicker.value = campaign ? String(campaign.id) : "";
    return;
  }
  perform(() => loadCampaign(Number(campaignPicker.value)), campaignStatus);
});

flowPicker.addEventListener("change", () => {
  if (!mayDiscard()) {
    flowPicker.value = flow ? String(flow.id) : "";
    return;
  }
  showFlow(campaign.flows.find((item) => item.id === Number(flowPicker.value)));
});

refreshFlows.addEventListener("click", () => {
  if (mayDiscard()) {
    perform(() => loadCatalog(Number(campaignPicker.value)), campaignStatus);
  }
});

offerSearch.addEventListener("input", updateSearch);
availableOffer.addEventListener("change", updateControls);
addOffer.addEventListener("click", () => {
  const offer = catalog.offers.find((item) => item.id === Number(availableOffer.value));
  if (offer) {
    changeOffers((next) => next.push({ ...offer, weight: 0, pinned: false, archived: false }));
  }
});

offerRows.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) {
    return;
  }
  const offerId = Number(button.dataset.id);
  if (button.dataset.action === "pin") {
    const offer = draft.find((item) => item.id === offerId);
    offer.pinned = !offer.pinned;
    message(offersStatus, "");
    renderOffers();
    return;
  }
  changeOffers((next) => {
    const offer = next.find((item) => item.id === offerId);
    offer.archived = button.dataset.action === "remove";
    offer.pinned = false;
    if (offer.archived) {
      offer.weight = 0;
    }
  });
});

resetOffers.addEventListener("click", () => showFlow(flow));
offersForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (saveOffers.disabled) {
    return;
  }
  perform(async () => {
    message(offersStatus, "Публикуем…");
    const offers = draft.filter((offer) => !offer.archived).map(({ id, weight, pinned }) => ({ id, weight, pinned }));
    const endpoint = "/api/campaigns/" + campaign.id + "/flows/" + flow.id + "/offers";
    const updated = await request(endpoint, "PUT", { offers, revision: flow.revision });
    campaign.flows = campaign.flows.map((item) => item.id === updated.id ? updated : item);
    showFlow(updated);
    message(offersStatus, "Офферы и доли опубликованы.", "success");
  }, offersStatus);
});

window.addEventListener("beforeunload", (event) => {
  if (isDirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

function initialize() {
  const notice = byId("catalog-notice");
  notice.hidden = false;
  byId("retry-catalog").hidden = false;
  message(byId("catalog-message"), "Загружаем справочники…");
  perform(async () => {
    try {
      await loadCatalog();
    } catch (error) {
      if (!notice.hidden) {
        throw error;
      }
      message(campaignStatus, error.message, "error");
    }
  }, byId("catalog-message"));
}

byId("retry-catalog").addEventListener("click", initialize);
initialize();
