"""Compact accessibility-tree snapshots with stable element refs.

Why a snapshot and not a screenshot / raw HTML:

* Raw HTML burns the context window and buries the signal under class names.
* A screenshot forces the model to OCR, is expensive, and — critically — gives
  the model no handles to act with.
* A ref-annotated text tree is small, greppable, and every actionable element
  gets a stable handle (`e12`) the agent can click/type into.

The DOM walk happens in the page (fast, one round trip) and returns flat node
records; Python does the formatting so it stays unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

REF_ATTR = "data-aiw-ref"
MAX_TEXT = 220
DEFAULT_CHAR_BUDGET = 14_000

_WS = re.compile(r"\s+")

#: Attributes worth surfacing because they carry domain meaning, not styling.
INTERESTING_DATA_ATTRS = (
    "data-invoice-number",
    "data-bill-id",
    "data-status",
    "data-amount",
    "data-currency",
    "data-due",
    "data-due-iso",
    "data-issued-iso",
    "data-error-for",
    "data-deferred-ready",
)

ROLE_BY_TAG = {
    "a": "link",
    "button": "button",
    "input": "textbox",
    "select": "combobox",
    "textarea": "textbox",
    "form": "form",
    "table": "table",
    "thead": "rowgroup",
    "tbody": "rowgroup",
    "tr": "row",
    "th": "columnheader",
    "td": "cell",
    "h1": "heading", "h2": "heading", "h3": "heading",
    "h4": "heading", "h5": "heading", "h6": "heading",
    "nav": "navigation",
    "main": "main",
    "header": "banner",
    "ul": "list", "ol": "list", "li": "listitem",
    "p": "paragraph",
    "label": "label",
    "option": "option",
    "dl": "definition-list", "dt": "term", "dd": "definition",
}

#: Tags whose *own* text is meaningful enough to print even when they have children.
TEXT_BEARING = {"p", "label", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "dt", "dd",
                "option", "summary", "caption", "legend"}

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "path", "head", "meta", "link"}


#: Runs in the page: walks visible elements, tags actionable ones with a ref.
SNAPSHOT_JS = r"""
(cfg) => {
  const REF_ATTR = cfg.refAttr;
  const INTERESTING = cfg.dataAttrs;
  const ROLE_BY_TAG = cfg.roleByTag;
  const SKIP = cfg.skipTags;

  // Clear refs from a previous snapshot so stale handles cannot be clicked.
  document.querySelectorAll('[' + REF_ATTR + ']').forEach(el => el.removeAttribute(REF_ATTR));

  const isVisible = (el) => {
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 && rect.height <= 0) {
      // Zero-size but focusable / form controls still matter.
      return false;
    }
    return true;
  };

  const directText = (el) => {
    let out = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) out += ' ' + node.textContent;
    }
    return out.replace(/\s+/g, ' ').trim();
  };

  const labelFor = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const byId = el.getAttribute('id');
    if (byId) {
      const lab = document.querySelector('label[for="' + CSS.escape(byId) + '"]');
      if (lab) return lab.textContent.replace(/\s+/g, ' ').trim();
    }
    const wrapping = el.closest('label');
    if (wrapping) return wrapping.textContent.replace(/\s+/g, ' ').trim();
    const ph = el.getAttribute('placeholder');
    if (ph) return ph.trim();
    if (el.tagName === 'BUTTON' || el.tagName === 'A') return directText(el);
    return '';
  };

  const nodes = [];
  let counter = 0;

  const walk = (el, depth) => {
    if (depth > 14) return;
    const tag = el.tagName.toLowerCase();
    if (SKIP.includes(tag)) return;
    if (!isVisible(el)) return;

    const role = el.getAttribute('role') || ROLE_BY_TAG[tag] || null;
    const actionable = ['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag)
      || el.hasAttribute('onclick')
      || role === 'button'
      || role === 'link';

    let ref = null;
    if (actionable) {
      ref = 'e' + (++counter);
      el.setAttribute(REF_ATTR, ref);
    }

    const dataAttrs = {};
    for (const name of INTERESTING) {
      const value = el.getAttribute(name);
      if (value !== null) dataAttrs[name] = value;
    }
    const idAttr = el.getAttribute('id');
    const testid = el.getAttribute('data-testid');

    const record = {
      depth,
      tag,
      role,
      ref,
      text: '',
      label: '',
      value: null,
      href: null,
      id: idAttr || null,
      testid: testid || null,
      type: el.getAttribute('type') || null,
      placeholder: el.getAttribute('placeholder') || null,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: el.checked === true ? true : (el.checked === false ? false : null),
      data: Object.keys(dataAttrs).length ? dataAttrs : null,
    };

    const own = directText(el);
    if (own) record.text = own;

    if (actionable) {
      record.label = labelFor(el);
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        record.value = el.value;
        if (tag === 'select') {
          const sel = el.options[el.selectedIndex];
          record.selected_text = sel ? sel.textContent.trim() : null;
          record.options = Array.from(el.options).map(o => o.value);
        }
      }
      if (tag === 'a') record.href = el.getAttribute('href');
    }

    // Only keep nodes that carry information.
    // Table and definition cells are always kept even when their text lives in a
    // child element: dropping them would silently shift every column to the left
    // for anyone reading the tree positionally.
    const structural = tag === 'table' || tag === 'tr' || tag === 'hr'
      || tag === 'td' || tag === 'th' || tag === 'dt' || tag === 'dd';
    const interesting = record.text || record.label || record.ref || record.data
      || structural;
    if (interesting) nodes.push(record);

    for (const child of el.children) walk(child, depth + 1);
  };

  walk(document.body, 0);

  return {
    title: document.title || '',
    url: location.href,
    nodes: nodes,
    total: counter,
  };
}
"""


@dataclass
class SnapshotNode:
    depth: int
    tag: str
    role: str | None
    ref: str | None
    text: str = ""
    label: str = ""
    value: Any = None
    href: str | None = None
    id: str | None = None
    testid: str | None = None
    type: str | None = None
    placeholder: str | None = None
    disabled: bool = False
    checked: bool | None = None
    data: dict[str, str] | None = None
    selected_text: str | None = None
    options: list[str] | None = None


@dataclass
class Snapshot:
    url: str
    title: str
    nodes: list[SnapshotNode] = field(default_factory=list)
    truncated: bool = False
    char_budget: int = DEFAULT_CHAR_BUDGET

    @property
    def refs(self) -> dict[str, SnapshotNode]:
        return {n.ref: n for n in self.nodes if n.ref}

    @property
    def text(self) -> str:
        return format_snapshot(self)

    @property
    def body_text(self) -> str:
        """All visible text, joined — useful for validation-error detection."""
        return " ".join(
            n.text for n in self.nodes if n.text
        )

    def find_by_ref(self, ref: str) -> SnapshotNode | None:
        return self.refs.get(ref)

    def find_by_label(self, needle: str) -> list[SnapshotNode]:
        low = needle.lower()
        return [
            n for n in self.nodes
            if low in (n.label or "").lower() or low in (n.text or "").lower()
        ]


def _clean(text: str, limit: int = MAX_TEXT) -> str:
    text = _WS.sub(" ", text or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _render_node(node: SnapshotNode) -> str:
    """One line per node: `kind "label" [e12] key=value`."""
    indent = "  " * node.depth
    bits: list[str] = []

    if node.ref:
        bits.append(f"[{node.ref}]")

    kind = node.role or node.tag
    if node.role is None and node.tag in {"div", "span", "b", "i", "em", "strong", "small",
                                          "section", "article", "aside", "figure"}:
        # Generic wrappers carry no semantics — label them as text so the output
        # reads like an accessibility tree rather than a tag dump.
        kind = "text"
    if node.tag == "a" and not node.href:
        kind = "text"
    label = node.label or node.text
    if label:
        bits.append(f'"{_clean(label)}"')
    elif node.tag in {"input", "select", "textarea"}:
        bits.append(f"<{node.tag}>")

    if node.tag == "input" and node.type:
        bits.append(f"type={node.type}")
    if node.value not in (None, ""):
        bits.append(f"value={_clean(str(node.value), 80)!r}")
    if node.selected_text:
        bits.append(f"selected={_clean(node.selected_text, 60)!r}")
    if node.options and len(node.options) <= 12:
        bits.append(f"options={node.options}")
    if node.href:
        bits.append(f"href={_clean(node.href, 90)}")
    if node.placeholder and not label:
        bits.append(f"placeholder={_clean(node.placeholder, 60)!r}")
    if node.testid:
        bits.append(f"testid={node.testid}")
    if node.checked is True:
        bits.append("checked")
    if node.disabled:
        bits.append("disabled")
    if node.data:
        pairs = " ".join(f"{k}={v}" for k, v in node.data.items())
        bits.append(_clean(pairs, 140))

    return f"{indent}{kind} " + " ".join(bits)


def format_snapshot(snapshot: Snapshot, char_budget: int | None = None) -> str:
    budget = char_budget or snapshot.char_budget
    lines = [f"url: {snapshot.url}", f"title: {snapshot.title}"]
    used = sum(len(line) for line in lines)

    for node in snapshot.nodes:
        line = _render_node(node)
        if used + len(line) + 1 > budget:
            remaining = len(snapshot.nodes) - len(lines) + 2
            lines.append(f"… snapshot truncated ({remaining} more nodes, raise the budget to see them)")
            snapshot.truncated = True
            break
        lines.append(line)
        used += len(line) + 1

    if not snapshot.nodes:
        lines.append("(page has no visible content)")
    return "\n".join(lines)


def parse_snapshot(raw: dict[str, Any], char_budget: int = DEFAULT_CHAR_BUDGET) -> Snapshot:
    nodes = [SnapshotNode(**node) for node in raw.get("nodes", [])]
    return Snapshot(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        nodes=nodes,
        char_budget=char_budget,
    )


def snapshot_config(ref_attr: str = REF_ATTR) -> dict[str, Any]:
    return {
        "refAttr": ref_attr,
        "dataAttrs": list(INTERESTING_DATA_ATTRS),
        "roleByTag": ROLE_BY_TAG,
        "skipTags": sorted(SKIP_TAGS),
    }