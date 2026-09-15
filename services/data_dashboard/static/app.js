// Common utility functions

function formatDate(d) {
    if (!d) return '';
    return d.slice(0, 10);
}

function formatDateTime(d) {
    if (!d) return '';
    return d.slice(0, 16);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showError(container, msg) {
    if (typeof container === 'string') container = document.getElementById(container);
    if (container) container.innerHTML = '<div class="error-message">' + (msg || '加载失败') + '</div>';
}

function showLoading(container) {
    if (typeof container === 'string') container = document.getElementById(container);
    if (container) container.innerHTML = '<div class="loading-spinner">加载中...</div>';
}
