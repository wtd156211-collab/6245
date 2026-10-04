// Tiny helper for element creation (no framework, no build step).
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else {
      node.setAttribute(k, v);
    }
  }
  for (const c of Array.isArray(children) ? children : [children]) {
    if (c != null) node.append(c);
  }
  return node;
}
export const create = el;
