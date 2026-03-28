/**
 * OBLIVION Search - Popup Script
 */

const SEARCH_URL = 'https://oblivionsearch.com/search?q=';
const HOME_URL = 'https://oblivionsearch.com/';

document.addEventListener('DOMContentLoaded', () => {
  const searchInput = document.getElementById('searchInput');
  const searchCountEl = document.getElementById('searchCount');
  const daysSinceEl = document.getElementById('daysSince');
  const linkHome = document.getElementById('linkHome');
  const linkNewTab = document.getElementById('linkNewTab');
  const linkSite = document.getElementById('linkSite');

  // Load stats
  chrome.runtime.sendMessage({ type: 'getStats' }, (response) => {
    if (response) {
      searchCountEl.textContent = formatNumber(response.searchCount || 0);
      const days = Math.floor((Date.now() - (response.installDate || Date.now())) / 86400000);
      daysSinceEl.textContent = Math.max(days, 1);
    }
  });

  // Search on Enter
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && searchInput.value.trim()) {
      const query = encodeURIComponent(searchInput.value.trim());
      chrome.tabs.create({ url: `${SEARCH_URL}${query}&src=popup` });
      chrome.runtime.sendMessage({ type: 'search' });
      window.close();
    }
  });

  // Quick links
  linkHome.addEventListener('click', (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: HOME_URL });
    window.close();
  });

  linkNewTab.addEventListener('click', (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: 'chrome://newtab/' });
    window.close();
  });

  linkSite.addEventListener('click', (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: HOME_URL });
    window.close();
  });

  // Focus search input
  searchInput.focus();
});

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
