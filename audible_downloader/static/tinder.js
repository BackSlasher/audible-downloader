// State
const STORAGE_KEY = 'audible-tinder-library';
let library = [];
let currentBook = null;
let currentFilter = 'all';

// DOM Elements
const authSection = document.getElementById('auth-section');
const controlsSection = document.getElementById('controls-section');
const gridSection = document.getElementById('grid-section');
const markingsSection = document.getElementById('markings-section');
const userInfo = document.getElementById('user-info');
const bookGrid = document.getElementById('book-grid');
const markingsList = document.getElementById('markings-list');
const statsEl = document.getElementById('stats');
const modal = document.getElementById('modal');

// Init
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    setupEventListeners();
});

function setupEventListeners() {
    // Control buttons
    document.getElementById('refresh-btn')?.addEventListener('click', refreshLibrary);
    document.getElementById('restore-later-btn')?.addEventListener('click', restoreLater);
    document.getElementById('view-markings-btn')?.addEventListener('click', showMarkings);
    document.getElementById('back-to-grid-btn')?.addEventListener('click', showGrid);
    document.getElementById('export-keeps-btn')?.addEventListener('click', exportKeeps);

    // Modal
    document.getElementById('modal-close')?.addEventListener('click', closeModal);
    document.querySelector('.modal-backdrop')?.addEventListener('click', closeModal);

    // Action buttons
    document.querySelectorAll('.action-btn').forEach(btn => {
        btn.addEventListener('click', () => handleAction(btn.dataset.action));
    });

    // Filter buttons
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentFilter = btn.dataset.filter;
            renderMarkings();
        });
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboard);

    // Touch swipe support for modal
    setupSwipeSupport();
}

function setupSwipeSupport() {
    let touchStartX = 0;
    let touchStartY = 0;
    const modalContent = document.querySelector('.modal-content');

    if (!modalContent) return;

    modalContent.addEventListener('touchstart', (e) => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
    }, { passive: true });

    modalContent.addEventListener('touchend', (e) => {
        if (!currentBook) return;

        const touchEndX = e.changedTouches[0].clientX;
        const touchEndY = e.changedTouches[0].clientY;
        const diffX = touchEndX - touchStartX;
        const diffY = touchEndY - touchStartY;

        // Minimum swipe distance
        const minSwipe = 80;

        // Horizontal swipe takes precedence
        if (Math.abs(diffX) > Math.abs(diffY) && Math.abs(diffX) > minSwipe) {
            if (diffX > 0) {
                handleAction('keep');
            } else {
                handleAction('delete');
            }
        } else if (Math.abs(diffY) > minSwipe && diffY > 0) {
            // Swipe down = later
            handleAction('later');
        }
    }, { passive: true });
}

function handleKeyboard(e) {
    if (!modal.classList.contains('hidden') && currentBook) {
        switch (e.key) {
            case 'ArrowRight':
                e.preventDefault();
                handleAction('keep');
                break;
            case 'ArrowLeft':
                e.preventDefault();
                handleAction('delete');
                break;
            case 'ArrowDown':
                e.preventDefault();
                handleAction('later');
                break;
            case 'Escape':
                closeModal();
                break;
        }
    }
}

// Auth
async function checkAuth() {
    try {
        const res = await fetch('/api/me');
        const data = await res.json();

        if (data.authenticated) {
            showLoggedIn(data.email);
            loadLibrary();
        } else {
            showLogin();
        }
    } catch (err) {
        console.error('Auth check failed:', err);
        showLogin();
    }
}

function showLogin() {
    authSection.classList.remove('hidden');
    controlsSection.classList.add('hidden');
    gridSection.classList.add('hidden');
    markingsSection.classList.add('hidden');
}

function showLoggedIn(email) {
    authSection.classList.add('hidden');
    controlsSection.classList.remove('hidden');
    gridSection.classList.remove('hidden');

    userInfo.innerHTML = `
        <span class="email">${escapeHtml(email)}</span>
        <a href="/" class="btn">Back to Main</a>
    `;
}

// Library Management
function loadLibrary() {
    // Try localStorage first
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
        try {
            library = JSON.parse(stored);
            renderGrid();
            updateStats();
            return;
        } catch (e) {
            console.error('Failed to parse stored library:', e);
        }
    }

    // Fetch from API
    refreshLibrary();
}

async function refreshLibrary() {
    bookGrid.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading library...</p></div>';

    try {
        const res = await fetch('/api/library?full=true');
        const data = await res.json();

        // Initialize with no decisions
        library = data.books.map(book => ({
            ...book,
            decision: null
        }));

        saveLibrary();
        renderGrid();
        updateStats();
    } catch (err) {
        bookGrid.innerHTML = '<div class="loading">Failed to load library. <button class="btn" onclick="refreshLibrary()">Retry</button></div>';
        console.error('Failed to load library:', err);
    }
}

function saveLibrary() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(library));
}

// Grid View
function showGrid() {
    gridSection.classList.remove('hidden');
    markingsSection.classList.add('hidden');
    renderGrid();
}

function renderGrid() {
    const undecided = library.filter(b => !b.decision);

    if (undecided.length === 0) {
        bookGrid.innerHTML = `
            <div class="empty-state">
                <p>All books sorted!</p>
                <button class="btn" onclick="document.getElementById('view-markings-btn').click()">View Markings</button>
            </div>
        `;
        return;
    }

    bookGrid.innerHTML = undecided.map(book => `
        <div class="tinder-card" data-asin="${book.asin}" onclick="openModal('${book.asin}')">
            <img src="${book.cover || ''}" alt="" onerror="this.style.background='#0f3460'">
            <div class="card-title">${escapeHtml(book.title)}</div>
        </div>
    `).join('');
}

function updateStats() {
    const keep = library.filter(b => b.decision === 'keep').length;
    const del = library.filter(b => b.decision === 'delete').length;
    const later = library.filter(b => b.decision === 'later').length;
    const undecided = library.filter(b => !b.decision).length;

    statsEl.innerHTML = `
        <span>${undecided} undecided</span>
        <span class="keep">${keep} keep</span>
        <span class="delete">${del} delete</span>
        <span class="later">${later} later</span>
    `;
}

// Modal
function openModal(asin) {
    currentBook = library.find(b => b.asin === asin);
    if (!currentBook) return;

    document.getElementById('modal-cover').src = currentBook.cover || '';
    document.getElementById('modal-title').textContent = currentBook.title;
    document.getElementById('modal-author').textContent = currentBook.author;
    document.getElementById('modal-runtime').textContent = currentBook.runtime;
    document.getElementById('modal-summary').textContent = currentBook.summary || 'No summary available.';

    const seriesEl = document.getElementById('modal-series');
    if (currentBook.series) {
        seriesEl.textContent = currentBook.series_num
            ? `${currentBook.series} #${currentBook.series_num}`
            : currentBook.series;
    } else {
        seriesEl.textContent = '';
    }

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closeModal() {
    modal.classList.add('hidden');
    document.body.style.overflow = '';
    currentBook = null;
}

function handleAction(action) {
    if (!currentBook) return;

    currentBook.decision = action;
    saveLibrary();
    closeModal();
    renderGrid();
    updateStats();
}

// Restore Later
function restoreLater() {
    let count = 0;
    library.forEach(book => {
        if (book.decision === 'later') {
            book.decision = null;
            count++;
        }
    });

    if (count > 0) {
        saveLibrary();
        renderGrid();
        updateStats();
    }
}

// Markings View
function showMarkings() {
    gridSection.classList.add('hidden');
    markingsSection.classList.remove('hidden');
    renderMarkings();
}

function renderMarkings() {
    let filtered = library;

    if (currentFilter === 'none') {
        filtered = library.filter(b => !b.decision);
    } else if (currentFilter !== 'all') {
        filtered = library.filter(b => b.decision === currentFilter);
    }

    if (filtered.length === 0) {
        markingsList.innerHTML = '<div class="empty-state"><p>No books in this category</p></div>';
        return;
    }

    markingsList.innerHTML = filtered.map(book => `
        <div class="marking-item" data-asin="${book.asin}">
            <img src="${book.cover || ''}" alt="" onerror="this.style.background='#0f3460'">
            <div class="marking-info">
                <div class="marking-title">${escapeHtml(book.title)}</div>
                <div class="marking-author">${escapeHtml(book.author)}</div>
            </div>
            <div class="marking-decision ${book.decision || 'none'}">${book.decision || 'undecided'}</div>
            <div class="marking-actions">
                <button class="keep-btn" onclick="setDecision('${book.asin}', 'keep')" title="Keep">&#10084;</button>
                <button class="delete-btn" onclick="setDecision('${book.asin}', 'delete')" title="Delete">&#10005;</button>
                <button class="later-btn" onclick="setDecision('${book.asin}', 'later')" title="Later">?</button>
                <button class="clear-btn" onclick="setDecision('${book.asin}', null)" title="Clear">&#8634;</button>
            </div>
        </div>
    `).join('');
}

function setDecision(asin, decision) {
    const book = library.find(b => b.asin === asin);
    if (book) {
        book.decision = decision;
        saveLibrary();
        renderMarkings();
        updateStats();
    }
}

// Export
function exportKeeps() {
    const keeps = library.filter(b => b.decision === 'keep');

    if (keeps.length === 0) {
        alert('No books marked as keep yet.');
        return;
    }

    const csv = [
        ['asin', 'title', 'author', 'series', 'url'].join(','),
        ...keeps.map(b => [
            b.asin,
            `"${(b.title || '').replace(/"/g, '""')}"`,
            `"${(b.author || '').replace(/"/g, '""')}"`,
            `"${(b.series || '').replace(/"/g, '""')}"`,
            `https://www.audible.com/pd/${b.asin}`
        ].join(','))
    ].join('\n');

    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'audible-keeps.csv';
    a.click();
    URL.revokeObjectURL(url);
}

// Helpers
function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>"']/g, m => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[m]));
}

// Expose to global for onclick handlers
window.openModal = openModal;
window.setDecision = setDecision;
window.refreshLibrary = refreshLibrary;
