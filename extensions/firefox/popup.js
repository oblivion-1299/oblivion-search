/**
 * OBLIVION Search - Popup Script (Firefox)
 */

const api = typeof browser !== 'undefined' ? browser : chrome;
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
  api.runtime.sendMessage({ type: 'getStats' }).then((response) => {
    if (response) {
      searchCountEl.textContent = formatNumber(response.searchCount || 0);
      const days = Math.floor((Date.now() - (response.installDate || Date.now())) / 86400000);
      daysSinceEl.textContent = Math.max(days, 1);
    }
  }).catch(() => {});

  // Search on Enter
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && searchInput.value.trim()) {
      const query = encodeURIComponent(searchInput.value.trim());
      api.tabs.create({ url: `${SEARCH_URL}${query}&src=popup` });
      api.runtime.sendMessage({ type: 'search' });
      window.close();
    }
  });

  // Quick links
  linkHome.addEventListener('click', (e) => {
    e.preventDefault();
    api.tabs.create({ url: HOME_URL });
    window.close();
  });

  linkNewTab.addEventListener('click', (e) => {
    e.preventDefault();
    api.tabs.create({});
    window.close();
  });

  linkSite.addEventListener('click', (e) => {
    e.preventDefault();
    api.tabs.create({ url: HOME_URL });
    window.close();
  });

  searchInput.focus();
});

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
