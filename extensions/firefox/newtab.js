/**
 * OBLIVION Search - New Tab Script (Firefox)
 */

const api = typeof browser !== 'undefined' ? browser : chrome;
const SEARCH_URL = 'https://oblivionsearch.com/search?q=';

document.addEventListener('DOMContentLoaded', () => {
  const searchInput = document.getElementById('searchInput');
  const searchBtn = document.getElementById('searchBtn');
  const searchCountEl = document.getElementById('searchCount');
  const trackersEl = document.getElementById('trackersBlocked');
  const daysSinceEl = document.getElementById('daysSince');
  const clockEl = document.getElementById('clock');
  const linkHome = document.getElementById('linkHome');

  // Load Stats
  api.runtime.sendMessage({ type: 'getStats' }).then((response) => {
    if (response) {
      const count = response.searchCount || 0;
      searchCountEl.textContent = formatNumber(count);
      trackersEl.textContent = formatNumber(count * 70);
      const days = Math.floor((Date.now() - (response.installDate || Date.now())) / 86400000);
      daysSinceEl.textContent = Math.max(days, 1);
    }
  }).catch(() => {});

  // Search
  function performSearch() {
    const query = searchInput.value.trim();
    if (!query) return;
    const encoded = encodeURIComponent(query);
    api.runtime.sendMessage({ type: 'search' });
    window.location.href = `${SEARCH_URL}${encoded}&src=newtab`;
  }

  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') performSearch();
  });

  searchBtn.addEventListener('click', performSearch);

  // Quick Links
  document.querySelectorAll('.quick-link[data-url]').forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      api.runtime.sendMessage({ type: 'search' });
      window.location.href = link.dataset.url;
    });
  });

  // Home Link
  linkHome.addEventListener('click', (e) => {
    e.preventDefault();
    window.location.href = 'https://oblivionsearch.com/';
  });

  // Clock
  function updateClock() {
    const now = new Date();
    const options = {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    };
    clockEl.textContent = now.toLocaleDateString(undefined, options);
  }

  updateClock();
  setInterval(updateClock, 30000);

  searchInput.focus();
});

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
