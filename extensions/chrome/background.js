/**
 * OBLIVION Search - Background Service Worker
 * Handles omnibox, context menus, keyboard shortcuts, and privacy counter.
 */

const SEARCH_URL = 'https://oblivionsearch.com/search?q=';
const HOME_URL = 'https://oblivionsearch.com/';

// ── Installation & Context Menu ──────────────────────────────────────────────

chrome.runtime.onInstalled.addListener((details) => {
  // Create context menu
  chrome.contextMenus.create({
    id: 'search-oblivion',
    title: chrome.i18n.getMessage('contextMenuSearch', '%s'),
    contexts: ['selection']
  });

  chrome.contextMenus.create({
    id: 'open-oblivion',
    title: chrome.i18n.getMessage('contextMenuOpen') || 'Open OBLIVION Search',
    contexts: ['page', 'frame']
  });

  // Initialize storage on first install
  if (details.reason === 'install') {
    chrome.storage.local.set({
      searchCount: 0,
      installDate: Date.now(),
      darkMode: true
    });
  }
});

// ── Context Menu Handler ─────────────────────────────────────────────────────

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'search-oblivion' && info.selectionText) {
    const query = encodeURIComponent(info.selectionText.trim());
    chrome.tabs.create({ url: `${SEARCH_URL}${query}&src=ctx` });
    incrementSearchCount();
  } else if (info.menuItemId === 'open-oblivion') {
    chrome.tabs.create({ url: HOME_URL });
  }
});

// ── Omnibox Handler ──────────────────────────────────────────────────────────

chrome.omnibox.onInputStarted.addListener(() => {
  chrome.omnibox.setDefaultSuggestion({
    description: 'Search OBLIVION for: %s'
  });
});

chrome.omnibox.onInputChanged.addListener((text, suggest) => {
  if (text.length < 2) return;

  // Provide suggestion hints
  suggest([
    {
      content: text,
      description: `Search OBLIVION for: <match>${escapeXml(text)}</match>`
    },
    {
      content: `${text} site:reddit.com`,
      description: `Search OBLIVION for: <match>${escapeXml(text)}</match> on Reddit`
    }
  ]);
});

chrome.omnibox.onInputEntered.addListener((text, disposition) => {
  const query = encodeURIComponent(text.trim());
  const url = `${SEARCH_URL}${query}&src=omni`;

  switch (disposition) {
    case 'currentTab':
      chrome.tabs.update({ url });
      break;
    case 'newForegroundTab':
      chrome.tabs.create({ url });
      break;
    case 'newBackgroundTab':
      chrome.tabs.create({ url, active: false });
      break;
  }

  incrementSearchCount();
});

// ── Privacy Counter ──────────────────────────────────────────────────────────

async function incrementSearchCount() {
  try {
    const data = await chrome.storage.local.get(['searchCount']);
    const count = (data.searchCount || 0) + 1;
    await chrome.storage.local.set({ searchCount: count });
    updateBadge(count);
  } catch (e) {
    // Silently handle storage errors
  }
}

function updateBadge(count) {
  if (count > 0) {
    const display = count >= 1000 ? `${Math.floor(count / 1000)}k` : String(count);
    chrome.action.setBadgeText({ text: display });
    chrome.action.setBadgeBackgroundColor({ color: '#8b5cf6' });
  }
}

// Restore badge on startup
chrome.runtime.onStartup.addListener(async () => {
  try {
    const data = await chrome.storage.local.get(['searchCount']);
    if (data.searchCount > 0) {
      updateBadge(data.searchCount);
    }
  } catch (e) {
    // Silently handle
  }
});

// ── Message Handler (from popup/newtab) ──────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'search') {
    incrementSearchCount();
    sendResponse({ ok: true });
  } else if (message.type === 'getStats') {
    chrome.storage.local.get(['searchCount', 'installDate']).then((data) => {
      sendResponse({
        searchCount: data.searchCount || 0,
        installDate: data.installDate || Date.now()
      });
    });
    return true; // Keep channel open for async response
  }
});

// ── "Compare with OBLIVION" -- detect Google search and show notification ────

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete' || !tab.url) return;

  try {
    const url = new URL(tab.url);
    // Detect Google search results pages
    if ((url.hostname === 'www.google.com' || url.hostname === 'google.com') &&
        url.pathname === '/search' && url.searchParams.has('q')) {
      const query = url.searchParams.get('q');
      if (query && query.trim().length > 0) {
        // Inject a small notification banner on the Google results page
        chrome.scripting.executeScript({
          target: { tabId: tabId },
          func: (searchQuery) => {
            // Don't show if already shown or dismissed
            if (document.getElementById('oblivion-compare-banner')) return;
            const dismissed = sessionStorage.getItem('oblivion_compare_dismissed');
            if (dismissed) return;

            const banner = document.createElement('div');
            banner.id = 'oblivion-compare-banner';
            banner.style.cssText = 'position:fixed;top:10px;right:10px;z-index:999999;background:linear-gradient(135deg,#1a1035,#0f0f1a);border:1px solid rgba(129,140,248,0.3);border-radius:12px;padding:14px 18px;max-width:320px;font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.4);animation:oblivionSlideIn 0.4s ease;';

            const style = document.createElement('style');
            style.textContent = '@keyframes oblivionSlideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}';
            document.head.appendChild(style);

            banner.innerHTML = `
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                <div style="font-size:14px;font-weight:700;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent">OBLIVION</div>
                <button id="oblivion-compare-close" style="margin-left:auto;background:none;border:none;color:#6b7280;font-size:18px;cursor:pointer;padding:0;line-height:1">&times;</button>
              </div>
              <div style="font-size:13px;color:#e4e4ec;margin-bottom:10px">See what OBLIVION found for this query</div>
              <a href="https://oblivionsearch.com/search?q=${encodeURIComponent(searchQuery)}&src=compare" target="_blank" style="display:block;text-align:center;padding:8px 16px;background:linear-gradient(135deg,#7c3aed,#6366f1);color:#fff;border-radius:8px;font-size:13px;font-weight:600;text-decoration:none;">Compare Results</a>
            `;
            document.body.appendChild(banner);

            document.getElementById('oblivion-compare-close').addEventListener('click', () => {
              banner.remove();
              sessionStorage.setItem('oblivion_compare_dismissed', '1');
            });

            // Auto-dismiss after 15 seconds
            setTimeout(() => { if (banner.parentNode) banner.remove(); }, 15000);
          },
          args: [query]
        }).catch(() => {
          // Permission denied on this page, silently ignore
        });
      }
    }
  } catch (e) {
    // Invalid URL, ignore
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
