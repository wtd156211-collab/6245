// viewkeep dashboard. Every rendered number is taken verbatim from data.json;
// this file never aggregates source data and never reads samples/.
import { create } from "./dom.js";

const data = await fetch("./data.json").then((r) => r.json());

const state = {
  step: data.watermark,
  view: data.views[0]?.view ?? null,
};

function checkpointAt(step) {
  return data.checkpoints.find((c) => c.step === step) ?? null;
}

function snapshotViewsAt(step) {
  const ck = checkpointAt(step);
  if (ck) return { views: ck.views, source: `卡片墙 = ${step} 步检查点全量` };
  return {
    views: data.views.map((v) => ({ view: v.view, rows: v.rows, vanished: v.vanished })),
    source: `卡片墙 = 最近检查点（${data.checkpoints.at(-1).step} 步）`,
  };
}

function keyText(key, columns) {
  return columns.map((c, i) => `${c}=${key[i]}`).join(" · ");
}

function valuesNode(values, columns) {
  const node = create("div", { class: "vals" });
  for (const col of columns) {
    node.append(create("span", { class: "k", text: col }),
                create("span", { class: "v", text: String(values[col]) }));
  }
  return node;
}

function renderCards(viewDef, snapshot) {
  const wall = document.querySelector("#cards");
  wall.replaceChildren();
  const live = snapshot.rows.map((r) => ({
    key: r.key,
    live: true,
    values: r.values,
  }));
  const gone = snapshot.vanished.map((e) => ({
    key: e.key,
    live: false,
    appeared: e.appeared_step,
    vanished: e.vanished_step,
    last: e.last_values,
  }));
  live.sort((a, b) => JSON.stringify(a.key).localeCompare(JSON.stringify(b.key)));
  for (const group of live) {
    wall.append(create("div", { class: "card" }, [
      create("div", { class: "key", text: keyText(group.key, viewDef.key_columns) }),
      valuesNode(group.values, viewDef.value_columns),
    ]));
  }
  for (const group of gone) {
    wall.append(create("div", { class: "card gone" }, [
      create("div", { class: "key" }, [
        document.createTextNode(keyText(group.key, viewDef.key_columns)),
        create("span", { class: "badge", text: "已消失" }),
      ]),
      create("div", {
        class: "life",
        text: `出现于 ${group.appeared} 步 · 消失于 ${group.vanished} 步`,
      }),
      valuesNode(group.last, viewDef.value_columns),
    ]));
  }
  if (!live.length && !gone.length) {
    wall.append(create("p", { class: "empty-note", text: "该视图暂无分组" }));
  }
}

function deltaRows(delta, kind) {
  if (!delta[kind].length) return null;
  const labels = { inserted: "新增", updated: "更新", deleted: "删除" };
  const cls = { inserted: "ins", updated: "upd", deleted: "del" };
  const block = create("div", {}, [create("div", {
    class: "delta-kind",
    text: `${labels[kind]} ${delta[kind].length} 行`,
  })]);
  for (const r of delta[kind]) {
    block.append(create("div", {
      class: `delta-row ${cls[kind]}`,
      text: `[${r.key.join(", ")}] ${kind === "updated"
        ? `${JSON.stringify(r.from)} → ${JSON.stringify(r.to)}`
        : JSON.stringify(r.values)}`,
    }));
  }
  return block;
}

function renderStepDetail(stepRec) {
  const body = document.querySelector("#step-detail-body");
  body.replaceChildren();
  body.append(create("p", {
    class: "ops-line",
    text: `本步 ${stepRec.ops} 个操作（step ${stepRec.step}）`,
  }));
  const names = Object.keys(stepRec.views).sort();
  if (!names.length) {
    body.append(create("p", { class: "empty-note", text: "本步没有视图行变化" }));
  }
  for (const name of names) {
    const delta = stepRec.views[name];
    const view = create("div", { class: "delta-view" }, [
      create("h3", { text: name }),
    ]);
    for (const kind of ["inserted", "updated", "deleted"]) {
      const node = deltaRows(delta, kind);
      if (node) view.append(node);
    }
    body.append(view);
  }
}

function render() {
  document.querySelector("#current-step").textContent = String(state.step);
  document.querySelector("#watermark").textContent = String(data.watermark);
  document.querySelector("#step-slider").value = String(state.step);
  document.querySelector("#step-input").value = String(state.step);

  const stepRec = data.steps.find((s) => s.step === state.step);
  renderStepDetail(stepRec ?? { step: state.step, ops: 0, views: {} });

  const { views, source } = snapshotViewsAt(state.step);
  document.querySelector("#wall-source").textContent = source;
  const viewDef = data.views.find((v) => v.view === state.view);
  const snapshot = views.find((v) => v.view === state.view);
  document.querySelector("#wall-title").textContent =
    `${state.view}（${snapshot.rows.length} 个当前分组，${snapshot.vanished.length} 个已消失）`;
  renderCards(viewDef, snapshot);
}

// view tabs
const tabs = document.querySelector("#view-tabs");
for (const v of data.views) {
  tabs.append(create("button", {
    text: v.view,
    class: v.view === state.view ? "active" : "",
    onClick: () => {
      state.view = v.view;
      tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      tabs.querySelector(`button[data-view="${v.view}"]`)?.classList.add("active");
      render();
    },
    "data-view": v.view,
  }));
}

function setStep(step) {
  state.step = Math.max(0, Math.min(data.watermark, Number(step)));
  render();
}
document.querySelector("#step-slider").addEventListener("input", (e) => setStep(e.target.value));
document.querySelector("#step-input").addEventListener("change", (e) => setStep(e.target.value));

render();
