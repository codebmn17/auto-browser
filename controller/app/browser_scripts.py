"""Browser-side JavaScript constants used by BrowserManager for page observation."""
from __future__ import annotations

INTERACTABLES_SCRIPT = r"""
(limit) => {
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function getLabel(el) {
    const raw = el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.innerText
      || el.value
      || el.getAttribute('name')
      || el.id
      || el.href
      || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  const selector = [
    'a',
    'button',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
    '[tabindex]'
  ].join(',');

  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: el.dataset.operatorId,
      selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label: getLabel(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      href: el.href || null,
      bbox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

ACTIVE_ELEMENT_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName.toLowerCase(),
    element_id: el.dataset?.operatorId || null,
    name: el.getAttribute('name'),
    id: el.id || null,
    label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.value || '').toString().replace(/\s+/g, ' ').trim().slice(0, 120)
  };
}
"""

PAGE_SUMMARY_SCRIPT = r"""
(textLimit) => {
  const squash = (value, maxLength = textLimit) =>
    String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .slice(0, 8)
    .map((el) => ({
      level: el.tagName.toLowerCase(),
      text: squash(el.innerText, 160)
    }))
    .filter((item) => item.text);

  const forms = Array.from(document.forms)
    .slice(0, 3)
    .map((form) => ({
      action: form.getAttribute('action') || null,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
        .slice(0, 8)
        .map((field) => ({
          tag: field.tagName.toLowerCase(),
          type: field.getAttribute('type') || null,
          name: field.getAttribute('name') || null,
          label: squash(
            field.getAttribute('aria-label')
              || field.getAttribute('placeholder')
              || field.innerText
              || field.value
              || field.getAttribute('name')
              || field.id,
            80
          ),
          disabled: Boolean(field.disabled || field.getAttribute('aria-disabled') === 'true')
        }))
    }));

  return {
    text_excerpt: squash(document.body?.innerText || '', textLimit),
    dom_outline: {
      headings,
      forms,
      counts: {
        links: document.querySelectorAll('a').length,
        buttons: document.querySelectorAll('button, [role="button"]').length,
        inputs: document.querySelectorAll('input, textarea, select').length,
        forms: document.forms.length
      }
    }
  };
}
"""

# Social interaction scripts — find and click engagement buttons
FIND_LIKE_BUTTON_SCRIPT = r"""
(postIndex) => {
  const likeSelectors = [
    '[data-testid="like"]',
    '[aria-label*="like" i]:not([aria-pressed="true"])',
    '[aria-label*="heart" i]:not([aria-pressed="true"])',
    '[aria-label*="love" i]:not([aria-pressed="true"])',
    'button[class*="like"]:not([class*="liked"])',
    'button[class*="heart"]',
    '[role="button"][class*="like"]',
  ];
  const allButtons = [];
  for (const sel of likeSelectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        allButtons.push({
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
        });
      }
    }
  }
  const target = allButtons[postIndex] || allButtons[0] || null;
  return target;
}
"""

FIND_FOLLOW_BUTTON_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="followButton"]',
    '[aria-label*="follow" i]:not([aria-label*="unfollow" i])',
    'button:not([class*="unfollow"]):not([class*="following"])',
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
      if (!text.includes('follow')) continue;
      if (text.includes('unfollow') || text.includes('following')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        return {
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80),
        };
      }
    }
  }
  return null;
}
"""

FIND_SEARCH_INPUT_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="SearchBox_Search_Input"]',
    'input[type="search"]',
    'input[name="q"]',
    '[aria-label*="search" i]',
    '[placeholder*="search" i]',
    'input[role="searchbox"]',
    '[role="searchbox"]',
    'input[type="text"][name*="search"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0) return sel;
    }
  }
  return null;
}
"""
