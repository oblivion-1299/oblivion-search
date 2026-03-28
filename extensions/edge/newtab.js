/**
 * OBLIVION Search - New Tab Script
 */

const SEARCH_URL = 'https://oblivionsearch.com/search?q=';

document.addEventListener('DOMContentLoaded', () => {
  const searchInput = document.getElementById('searchInput');
  const searchBtn = document.getElementById('searchBtn');
  const searchCountEl = document.getElementById('searchCount');
  const trackersEl = document.getElementById('trackersBlocked');
  const daysSinceEl = document.getElementById('daysSince');
  const clockEl = document.getElementById('clock');
  const linkHome = document.getElementById('linkHome');

  // ── Load Stats ───────────────────────────────────────────────────────────

  chrome.runtime.sendMessage({ type: 'getStats' }, (response) => {
    if (response) {
      const count = response.searchCount || 0;
      searchCountEl.textContent = formatNumber(count);
      // Estimated trackers avoided (avg ~70 per search on other engines)
      trackersEl.textContent = formatNumber(count * 70);
      const days = Math.floor((Date.now() - (response.installDate || Date.now())) / 86400000);
      daysSinceEl.textContent = Math.max(days, 1);
    }
  });

  // ── Search ───────────────────────────────────────────────────────────────

  function performSearch() {
    const query = searchInput.value.trim();
    if (!query) return;
    const encoded = encodeURIComponent(query);
    chrome.runtime.sendMessage({ type: 'search' });
    window.location.href = `${SEARCH_URL}${encoded}&src=newtab`;
  }

  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') performSearch();
  });

  searchBtn.addEventListener('click', performSearch);

  // ── Quick Links ──────────────────────────────────────────────────────────

  document.querySelectorAll('.quick-link[data-url]').forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      chrome.runtime.sendMessage({ type: 'search' });
      window.location.href = link.dataset.url;
    });
  });

  // ── Home Link ────────────────────────────────────────────────────────────

  linkHome.addEventListener('click', (e) => {
    e.preventDefault();
    window.location.href = 'https://oblivionsearch.com/';
  });

  // ── Clock ────────────────────────────────────────────────────────────────

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

  // ── Focus ────────────────────────────────────────────────────────────────

  searchInput.focus();
});

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
