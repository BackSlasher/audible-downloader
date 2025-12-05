// State
let currentUser = null;
let library = [];
let selectedBooks = new Set();
let jobsInterval = null;

// DOM Elements
const authSection = document.getElementById('auth-section');
const librarySection = document.getElementById('library-section');
const jobsSection = document.getElementById('jobs-section');
const downloadsSection = document.getElementById('downloads-section');
const userInfo = document.getElementById('user-info');
const libraryGrid = document.getElementById('library-grid');
const jobsList = document.getElementById('jobs-list');
const downloadsList = document.getElementById('downloads-list');

// Init
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    setupEventListeners();
});

function setupEventListeners() {
    document.getElementById('login-btn')?.addEventListener('click', startLogin);
    document.getElementById('complete-auth-btn')?.addEventListener('click', completeLogin);
    document.getElementById('refresh-btn')?.addEventListener('click', () => loadLibrary(true));
    document.getElementById('download-selected-btn')?.addEventListener('click', downloadSelected);
    document.getElementById('library-filter')?.addEventListener('input', (e) => renderLibrary(e.target.value));
}

// Auth
async function checkAuth() {
    try {
        const res = await fetch('/api/me');
        const data = await res.json();

        if (data.authenticated) {
            currentUser = data;
            showLoggedIn();
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
    librarySection.classList.add('hidden');
    jobsSection.classList.add('hidden');
    downloadsSection.classList.add('hidden');
    userInfo.innerHTML = '';
}

function showLoggedIn() {
    authSection.classList.add('hidden');
    librarySection.classList.remove('hidden');
    jobsSection.classList.remove('hidden');
    downloadsSection.classList.remove('hidden');

    userInfo.innerHTML = `
        <span class="email">${currentUser.email}</span>
        <button class="btn danger" onclick="logout()">Logout</button>
    `;

    loadLibrary();
    loadJobs();
    loadDownloads();

    // Poll jobs every 3 seconds
    if (jobsInterval) clearInterval(jobsInterval);
    jobsInterval = setInterval(loadJobs, 3000);
}

async function startLogin() {
    const locale = document.getElementById('locale').value;

    try {
        const res = await fetch(`/api/auth/start?locale=${locale}`);
        const data = await res.json();

        // Clear any previous callback URL
        document.getElementById('callback-url').value = '';

        // Open OAuth URL in new window
        window.open(data.url, '_blank');

        // Show instructions
        document.getElementById('auth-instructions').classList.remove('hidden');

        console.log('Login started at:', new Date().toISOString());
    } catch (err) {
        alert('Failed to start login: ' + err.message);
    }
}

async function completeLogin() {
    const callbackUrl = document.getElementById('callback-url').value.trim();

    if (!callbackUrl) {
        alert('Please paste the callback URL');
        return;
    }

    try {
        const res = await fetch(`/api/auth/callback?response_url=${encodeURIComponent(callbackUrl)}`);
        const data = await res.json();

        if (res.ok && data.success) {
            currentUser = { email: data.email, authenticated: true };
            showLoggedIn();
        } else {
            alert('Login failed: ' + (data.detail || JSON.stringify(data)));
        }
    } catch (err) {
        alert('Login failed: ' + err.message);
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
        currentUser = null;
        if (jobsInterval) clearInterval(jobsInterval);
        showLogin();
    } catch (err) {
        console.error('Logout failed:', err);
    }
}

// Library
async function loadLibrary(refresh = false) {
    libraryGrid.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading library...</p></div>';

    try {
        const url = refresh ? '/api/library?refresh=true' : '/api/library';
        const res = await fetch(url);
        const data = await res.json();
        library = data.books;
        const filterValue = document.getElementById('library-filter')?.value || '';
        renderLibrary(filterValue);
    } catch (err) {
        libraryGrid.innerHTML = '<div class="loading">Failed to load library</div>';
        console.error('Failed to load library:', err);
    }
}

function renderLibrary(filter = '') {
    const filterLower = filter.toLowerCase();
    const filteredBooks = filter
        ? library.filter(book =>
            (book.title || '').toLowerCase().includes(filterLower) ||
            (book.author || '').toLowerCase().includes(filterLower))
        : library;

    libraryGrid.innerHTML = filteredBooks.map(book => `
        <div class="book-card ${book.downloaded ? 'downloaded' : ''} ${selectedBooks.has(book.asin) ? 'selected' : ''}"
             data-asin="${book.asin}"
             onclick="toggleBook('${book.asin}')">
            <img class="book-cover" src="${book.cover || ''}" alt="" onerror="this.style.display='none'">
            <div class="book-info">
                <div class="book-title">${escapeHtml(book.title)}</div>
                <div class="book-author">${escapeHtml(book.author)}</div>
                <div class="book-runtime">${book.runtime}</div>
            </div>
        </div>
    `).join('');

    if (filteredBooks.length === 0 && filter) {
        libraryGrid.innerHTML = '<div class="loading">No books match your filter</div>';
    }
}

function toggleBook(asin) {
    const card = document.querySelector(`[data-asin="${asin}"]`);
    const book = library.find(b => b.asin === asin);

    if (book.downloaded) return; // Don't select already downloaded books

    if (selectedBooks.has(asin)) {
        selectedBooks.delete(asin);
        card.classList.remove('selected');
    } else {
        selectedBooks.add(asin);
        card.classList.add('selected');
    }

    updateDownloadButton();
}

function updateDownloadButton() {
    const btn = document.getElementById('download-selected-btn');
    btn.disabled = selectedBooks.size === 0;
    btn.textContent = selectedBooks.size > 0
        ? `Download Selected (${selectedBooks.size})`
        : 'Download Selected';
}

async function downloadSelected() {
    if (selectedBooks.size === 0) return;

    try {
        const res = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ asins: Array.from(selectedBooks) })
        });

        const data = await res.json();

        // Clear selection
        selectedBooks.clear();
        document.querySelectorAll('.book-card.selected').forEach(card => {
            card.classList.remove('selected');
        });
        updateDownloadButton();

        // Refresh jobs
        loadJobs();

    } catch (err) {
        alert('Failed to start download: ' + err.message);
    }
}

// Jobs
async function loadJobs() {
    try {
        const res = await fetch('/api/jobs');
        const data = await res.json();
        renderJobs(data.jobs);
    } catch (err) {
        console.error('Failed to load jobs:', err);
    }
}

function renderJobs(jobs) {
    if (jobs.length === 0) {
        jobsList.innerHTML = '<div class="loading">No active downloads</div>';
        return;
    }

    jobsList.innerHTML = jobs.map(job => `
        <div class="job-item">
            <div class="job-info">
                <div class="job-title">${escapeHtml(job.title)}</div>
                <span class="job-status ${job.status}">${job.status}</span>
                ${job.error ? `<div style="color: #c0392b; font-size: 12px; margin-top: 5px;">${escapeHtml(job.error)}</div>` : ''}
            </div>
            <div class="job-actions">
                ${job.status === 'running' ? `
                    <div class="progress-bar">
                        <div class="progress-bar-fill" style="width: ${job.progress}%"></div>
                    </div>
                ` : `
                    <button class="btn danger small" onclick="deleteJob(${job.id})">Delete</button>
                `}
            </div>
        </div>
    `).join('');
}

async function deleteJob(jobId) {
    if (!confirm('Delete this job?')) return;
    try {
        await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
        loadJobs();
    } catch (err) {
        alert('Failed to delete job: ' + err.message);
    }
}

// Downloads
async function loadDownloads() {
    try {
        const res = await fetch('/api/books');
        const data = await res.json();
        renderDownloads(data.books);
    } catch (err) {
        console.error('Failed to load downloads:', err);
    }
}

function renderDownloads(books) {
    if (books.length === 0) {
        downloadsList.innerHTML = '<div class="loading">No downloaded books yet</div>';
        return;
    }

    downloadsList.innerHTML = books.map(book => `
        <div class="download-item">
            <div class="download-info">
                <div class="download-title">${escapeHtml(book.title)}</div>
                <div class="download-author">${escapeHtml(book.author || '')}</div>
            </div>
            <div class="download-actions">
                <a href="/api/download/${book.asin}" class="btn primary">Download ZIP</a>
                <button class="btn danger small" onclick="deleteBook(${book.id}, '${escapeHtml(book.title)}')">Delete</button>
            </div>
        </div>
    `).join('');
}

async function deleteBook(bookId, title) {
    if (!confirm(`Delete "${title}" and all its files?`)) return;
    try {
        await fetch(`/api/books/${bookId}`, { method: 'DELETE' });
        loadDownloads();
    } catch (err) {
        alert('Failed to delete book: ' + err.message);
    }
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

