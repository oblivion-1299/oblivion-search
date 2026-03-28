/**
 * OBLIVION Search - Background Script (Firefox)
 * Uses browser.* API with polyfill fallback to chrome.*
 */

const api = typeof browser !== 'undefined' ? browser : chrome;
const SEARCH_URL = 'https://oblivionsearch.com/search?q=';
const HOME_URL = 'https://oblivionsearch.com/';

// ── Installation & Context Menu ──────────────────────────────────────────────

api.runtime.onInstalled.addListener((details) => {
  // Create context menus
  api.contextMenus.create({
    id: 'search-oblivion',
    title: api.i18n.getMessage('contextMenuSearch', '%s'),
    contexts: ['selection']
  });

  api.contextMenus.create({
    id: 'open-oblivion',
    title: api.i18n.getMessage('contextMenuOpen') || 'Open OBLIVION Search',
    contexts: ['page', 'frame']
  });

  // Initialize storage on first install
  if (details.reason === 'install') {
    api.storage.local.set({
      searchCount: 0,
      installDate: Date.now(),
      darkMode: true
    });
  }
});

// ── Context Menu Handler ─────────────────────────────────────────────────────

api.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'search-oblivion' && info.selectionText) {
    const query = encodeURIComponent(info.selectionText.trim());
    api.tabs.create({ url: `${SEARCH_URL}${query}&src=ctx` });
    incrementSearchCount();
  } else if (info.menuItemId === 'open-oblivion') {
    api.tabs.create({ url: HOME_URL });
  }
});

// ── Omnibox Handler ──────────────────────────────────────────────────────────

api.omnibox.onInputStarted.addListener(() => {
  api.omnibox.setDefaultSuggestion({
    description: 'Search OBLIVION for: %s'
  });
});

api.omnibox.onInputChanged.addListener((text, suggest) => {
  if (text.length < 2) return;

  suggest([
    {
      content: text,
      description: `Search OBLIVION for: ${escapeXml(text)}`
    },
    {
      content: `${text} site:reddit.com`,
      description: `Search OBLIVION for: ${escapeXml(text)} on Reddit`
    }
  ]);
});

api.omnibox.onInputEntered.addListener((text, disposition) => {
  const query = encodeURIComponent(text.trim());
  const url = `${SEARCH_URL}${query}&src=omni`;

  switch (disposition) {
    case 'currentTab':
      api.tabs.update({ url });
      break;
    case 'newForegroundTab':
      api.tabs.create({ url });
      break;
    case 'newBackgroundTab':
      api.tabs.create({ url, active: false });
      break;
  }

  incrementSearchCount();
});

// ── Privacy Counter ──────────────────────────────────────────────────────────

function incrementSearchCount() {
  api.storage.local.get(['searchCount']).then((data) => {
    const count = (data.searchCount || 0) + 1;
    api.storage.local.set({ searchCount: count });
    updateBadge(count);
  }).catch(() => {});
}

function updateBadge(count) {
  if (count > 0) {
    const display = count >= 1000 ? `${Math.floor(count / 1000)}k` : String(count);
    api.browserAction.setBadgeText({ text: display });
    api.browserAction.setBadgeBackgroundColor({ color: '#8b5cf6' });
  }
}

// ── Message Handler ──────────────────────────────────────────────────────────

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'search') {
    incrementSearchCount();
    sendResponse({ ok: true });
  } else if (message.type === 'getStats') {
    api.storage.local.get(['searchCount', 'installDate']).then((data) => {
      sendResponse({
        searchCount: data.searchCount || 0,
        installDate: data.installDate || Date.now()
      });
    });
    return true; // Keep channel open for async
  }
});

// ── Utilities ────────────────────────────────────────────────────────────────

function escapeXml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
