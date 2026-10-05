// viewkeep 看板:所有数字直接取自 data.json,不重新计算任何聚合。
const data = await (await fetch("./data.json")).json();

const rangeEl = document.getElementById("step-range");
const inputEl = document.getElementById("step-input");
const hintEl = document.getElementById("step-hint");
const detailEl = document.getElementById("step-detail");
const viewsEl = document.getElementById("views");

document.getElementById("watermark").textContent = data.watermark;
rangeEl.max = data.watermark;
inputEl.max = data.watermark;
rangeEl.value = data.watermark;
inputEl.value = data.watermark;

const checkpointByStep = new Map(
  data.checkpoints.map((cp) => [cp.step, cp])
);

function fmtKey(key) {
  return key.join(" / ");
}

function keyHeader(view, prefix) {
  return view.key_columns.length
    ? view.key_columns.join(" / ")
    : prefix;
}

function renderValues(view, values) {
  return view.value_columns.map((name) => {
    const row = document.createElement("div");
    row.style.display = "contents";
    const n = document.createElement("span");
    n.className = "v-name";
    n.textContent = name;
    const v = document.createElement("span");
    v.className = "v-val";
    v.textContent = String(values[name]);
    row.append(n, v);
    return row;
  });
}

function renderCard(view, item, vanished) {
  const card = document.createElement("article");
  card.className = "card" + (vanished ? " vanished" : "");
  const key = document.createElement("div");
  key.className = "key";
  key.textContent = fmtKey(item.key);
  const vals = document.createElement("div");
  vals.className = "vals";
  const values = vanished ? item.last_values : item.values;
  renderValues(view, values).forEach((nodes) => vals.append(nodes));
  card.append(key, vals);
  if (vanished) {
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent =
      `第 ${item.appeared_step} 步出现 · 第 ${item.vanished_step} 步变空`;
    card.append(meta);
  }
  return card;
}

function renderWall(step) {
  const checkpoint = checkpointByStep.get(step);
  const sourceParts = checkpoint
    ? checkpoint.views
    : data.views;
  hintEl.textContent = checkpoint
    ? `第 ${step} 步是检查点,卡片墙为该步全量快照`
    : `第 ${step} 步不是检查点,卡片墙保持最近检查点(第 ${data.checkpoints.at(-1).step} 步)全量`;

  viewsEl.textContent = "";
  for (const view of data.views) {
    const part = sourceParts.find((p) => p.view === view.view);
    const block = document.createElement("section");
    block.className = "view-block";
    const head = document.createElement("div");
    head.className = "view-head";
    const h2 = document.createElement("h2");
    h2.textContent = view.view;
    const info = document.createElement("span");
    info.textContent =
      `分组键: ${keyHeader(view, "(无)")} · ` +
      `${part.rows.length} 个现存分组 · ${part.vanished.length} 个已消失`;
    head.append(h2, info);

    const cards = document.createElement("div");
    cards.className = "cards";
    for (const row of part.rows) {
      cards.append(renderCard(view, row, false));
    }
    for (const gone of part.vanished) {
      cards.append(renderCard(view, gone, true));
    }
    block.append(head, cards);
    viewsEl.append(block);
  }
}

function deltaEntries(delta, kind, tag) {
  const col = document.createElement("div");
  col.className = "delta-col";
  const head = document.createElement("span");
  head.className = "tag " + kind;
  head.textContent = `${tag} ${delta[kind].length}`;
  const ul = document.createElement("ul");
  for (const item of delta[kind]) {
    const li = document.createElement("li");
    if (kind === "updated") {
      li.textContent =
        `${fmtKey(item.key)}: ${JSON.stringify(item.from)} → ${JSON.stringify(item.to)}`;
    } else {
      li.textContent =
        `${fmtKey(item.key)} ${JSON.stringify(item.values)}`;
    }
    ul.append(li);
  }
  col.append(head, ul);
  return col;
}

function renderStepDetail(step) {
  detailEl.textContent = "";
  const h2 = document.createElement("h2");
  if (step === 0) {
    h2.textContent = "第 0 步:build 初始快照,无变更操作";
    detailEl.append(h2);
    return;
  }
  const record = data.steps[step - 1];
  h2.textContent = `第 ${step} 步:${record.ops} 条操作`;
  detailEl.append(h2);
  const names = Object.keys(record.views);
  if (names.length === 0) {
    const p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = "该步没有视图行变化(可能只改了未引用列或净效果抵消)。";
    detailEl.append(p);
    return;
  }
  for (const name of names) {
    const delta = record.views[name];
    const block = document.createElement("div");
    block.className = "delta-view";
    const h3 = document.createElement("h3");
    h3.textContent = name;
    const lists = document.createElement("div");
    lists.className = "delta-lists";
    lists.append(
      deltaEntries(delta, "inserted", "新增"),
      deltaEntries(delta, "updated", "更新"),
      deltaEntries(delta, "deleted", "删除"),
    );
    block.append(h3, lists);
    detailEl.append(block);
  }
}

function selectStep(step) {
  step = Math.max(0, Math.min(data.watermark, Number(step) || 0));
  document.getElementById("cur-step").textContent = step;
  rangeEl.value = step;
  inputEl.value = step;
  renderStepDetail(step);
  renderWall(step);
}

rangeEl.addEventListener("input", () => selectStep(rangeEl.value));
inputEl.addEventListener("change", () => selectStep(inputEl.value));

selectStep(data.watermark);
