"use strict";

function camelToDataAttr(prop) {
  return (
    "data-" +
    String(prop).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)
  );
}

function dataAttrToCamel(attr) {
  return String(attr)
    .slice(5)
    .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

class TestTextNode {
  constructor(text, ownerDocument) {
    this.nodeType = 3;
    this.nodeValue = String(text ?? "");
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
  }

  get textContent() {
    return this.nodeValue;
  }

  set textContent(v) {
    this.nodeValue = String(v ?? "");
  }
}

class TestClassList {
  constructor(el) {
    this._el = el;
    this._set = new Set();
  }

  _syncFromString(str) {
    this._set = new Set(
      String(str || "")
        .split(/\s+/)
        .map((s) => s.trim())
        .filter(Boolean)
    );
  }

  _toString() {
    return [...this._set].join(" ");
  }

  add(...tokens) {
    for (const t of tokens) {
      if (t) this._set.add(String(t));
    }
    this._el._syncClassAttr();
  }

  remove(...tokens) {
    for (const t of tokens) {
      this._set.delete(String(t));
    }
    this._el._syncClassAttr();
  }

  contains(token) {
    return this._set.has(String(token));
  }

  toggle(token, force) {
    const key = String(token);
    const shouldHave = force !== undefined ? Boolean(force) : !this._set.has(key);
    if (shouldHave) this._set.add(key);
    else this._set.delete(key);
    this._el._syncClassAttr();
    return shouldHave;
  }
}

function matchesSimpleSelector(el, selector) {
  if (!el || el.nodeType !== 1) return false;
  let rem = selector.trim();
  if (!rem) return false;

  const attrMatches = [];
  rem = rem.replace(/\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]/g, (_, k, v) => {
    attrMatches.push({ k, v });
    return "";
  });

  let idReq = null;
  rem = rem.replace(/#([a-zA-Z0-9_-]+)/g, (_, id) => {
    idReq = id;
    return "";
  });

  const classes = [];
  rem = rem.replace(/\.([a-zA-Z0-9_-]+)/g, (_, cls) => {
    classes.push(cls);
    return "";
  });

  const tagReq = rem.trim().toLowerCase();
  if (tagReq && tagReq !== "*" && el.tagName.toLowerCase() !== tagReq) {
    return false;
  }
  if (idReq && el.id !== idReq) {
    return false;
  }
  for (const cls of classes) {
    if (!el.classList.contains(cls)) return false;
  }
  for (const { k, v } of attrMatches) {
    if (!el.hasAttribute(k)) return false;
    if (v !== undefined && el.getAttribute(k) !== v) return false;
  }
  return true;
}

function matchesSelectorChain(el, fullSelector) {
  const parts = String(fullSelector || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return false;
  if (!matchesSimpleSelector(el, parts[parts.length - 1])) return false;

  let cur = el.parentNode;
  for (let i = parts.length - 2; i >= 0; i -= 1) {
    while (cur && cur.nodeType === 1 && !matchesSimpleSelector(cur, parts[i])) {
      cur = cur.parentNode;
    }
    if (!cur || cur.nodeType !== 1) return false;
    cur = cur.parentNode;
  }
  return true;
}

class TestElement {
  constructor(tagName, ownerDocument, namespaceURI = null) {
    this.nodeType = 1;
    this.tagName = namespaceURI
      ? String(tagName)
      : String(tagName).toUpperCase();
    this.namespaceURI = namespaceURI;
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.childNodes = [];
    this.attributes = new Map();
    this.style = {};
    this.listeners = new Map();
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this.value = "";
    this.title = "";
    this.id = "";
    this._scrolledIntoViewCount = 0;

    this.classList = new TestClassList(this);
    const self = this;
    this.dataset = new Proxy(
      {},
      {
        get(_target, prop) {
          if (typeof prop !== "string") return undefined;
          const attr = camelToDataAttr(prop);
          return self.attributes.has(attr) ? self.attributes.get(attr) : undefined;
        },
        set(_target, prop, val) {
          if (typeof prop === "string") {
            self.setAttribute(camelToDataAttr(prop), String(val));
          }
          return true;
        },
      }
    );
  }

  _syncClassAttr() {
    const str = this.classList._toString();
    if (str) this.attributes.set("class", str);
    else this.attributes.delete("class");
  }

  get className() {
    return this.classList._toString();
  }

  set className(val) {
    this.classList._syncFromString(val);
    this._syncClassAttr();
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }

  get isConnected() {
    let cur = this;
    while (cur) {
      if (cur === this.ownerDocument?.body || cur === this.ownerDocument) {
        return true;
      }
      cur = cur.parentNode;
    }
    return false;
  }

  get textContent() {
    return this.childNodes.map((c) => c.textContent).join("");
  }

  set textContent(val) {
    for (const c of this.childNodes) {
      c.parentNode = null;
    }
    this.childNodes = [];
    const str = String(val ?? "");
    if (str.length > 0) {
      this.appendChild(new TestTextNode(str, this.ownerDocument));
    }
  }

  setAttribute(name, val) {
    const k = String(name);
    const v = String(val ?? "");
    this.attributes.set(k, v);
    if (k === "class") {
      this.classList._syncFromString(v);
    } else if (k === "id") {
      this.id = v;
    }
  }

  getAttribute(name) {
    const k = String(name);
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(String(name));
  }

  removeAttribute(name) {
    const k = String(name);
    this.attributes.delete(k);
    if (k === "class") this.classList._syncFromString("");
    if (k === "id") this.id = "";
  }

  appendChild(node) {
    if (!node) return node;
    if (node.parentNode) {
      node.parentNode.removeChild(node);
    }
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx >= 0) {
      this.childNodes.splice(idx, 1);
      node.parentNode = null;
    }
    return node;
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  replaceChildren(...nodes) {
    for (const c of this.childNodes) {
      c.parentNode = null;
    }
    this.childNodes = [];
    for (const n of nodes) {
      if (typeof n === "string") {
        this.appendChild(new TestTextNode(n, this.ownerDocument));
      } else if (n) {
        this.appendChild(n);
      }
    }
  }

  addEventListener(type, fn) {
    const k = String(type);
    if (!this.listeners.has(k)) this.listeners.set(k, []);
    this.listeners.get(k).push(fn);
  }

  removeEventListener(type, fn) {
    const arr = this.listeners.get(String(type));
    if (!arr) return;
    const idx = arr.indexOf(fn);
    if (idx >= 0) arr.splice(idx, 1);
  }

  dispatchEvent(evt = {}) {
    const eventObj = {
      type: evt.type || "event",
      target: evt.target || this,
      currentTarget: this,
      defaultPrevented: false,
      propagationStopped: false,
      ...evt,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {
        this.propagationStopped = true;
      },
    };
    const fns = [...(this.listeners.get(eventObj.type) || [])];
    for (const fn of fns) {
      fn(eventObj);
    }
    if (typeof this[`on${eventObj.type}`] === "function") {
      this[`on${eventObj.type}`](eventObj);
    }
    return !eventObj.defaultPrevented;
  }

  click(extra = {}) {
    return this.dispatchEvent({ type: "click", button: 0, ...extra });
  }

  scrollIntoView() {
    this._scrolledIntoViewCount += 1;
  }

  querySelectorAll(selectorGroup) {
    const selectors = String(selectorGroup || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const out = [];
    const visit = (node) => {
      for (const child of node.children || []) {
        if (selectors.some((sel) => matchesSelectorChain(child, sel))) {
          out.push(child);
        }
        visit(child);
      }
    };
    visit(this);
    return out;
  }

  querySelector(selectorGroup) {
    return this.querySelectorAll(selectorGroup)[0] || null;
  }
}

// Poison unsafe HTML sinks on TestElement so any accidental usage fails immediately in tests
for (const sink of [
  "inner" + "HTML",
  "outer" + "HTML",
  "insertAdjacent" + "HTML",
]) {
  Object.defineProperty(TestElement.prototype, sink, {
    get() {
      throw new Error(`Security violation: ${sink} access is forbidden`);
    },
    set() {
      throw new Error(`Security violation: ${sink} assignment is forbidden`);
    },
  });
}

class TestDocument {
  constructor() {
    this.nodeType = 9;
    this.body = new TestElement("body", this);
    this.body.parentNode = this;
  }

  createElement(tag) {
    return new TestElement(tag, this, null);
  }

  createElementNS(ns, tag) {
    return new TestElement(tag, this, ns);
  }

  createTextNode(text) {
    return new TestTextNode(text, this);
  }

  querySelectorAll(sel) {
    return this.body.querySelectorAll(sel);
  }

  querySelector(sel) {
    return this.body.querySelector(sel);
  }

  getElementById(id) {
    return this.body.querySelector(`#${id}`);
  }
}

Object.defineProperty(TestDocument.prototype, "write", {
  value() {
    throw new Error("Security violation: document.write is forbidden");
  },
});

function createTestDom() {
  const doc = new TestDocument();
  return { doc, body: doc.body };
}

module.exports = {
  createTestDom,
  TestDocument,
  TestElement,
  TestTextNode,
  dataAttrToCamel,
};
