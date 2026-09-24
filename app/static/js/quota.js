function formatQuotaResetTimes() {
    const SECOND_MS = 1000;
    const MINUTE_MS = 60 * SECOND_MS;
    const HOUR_MS = 60 * MINUTE_MS;
    const DAY_MS = 24 * HOUR_MS;
    const EPOCH_MILLISECONDS_MIN = 1_000_000_000_000;

    document.querySelectorAll('.quota-reset-time').forEach((element) => {
        const raw = element.dataset.resetAt || '';
        const numeric = Number(raw);
        const timestamp = Number.isFinite(numeric) && raw.trim() !== ''
            ? (numeric >= EPOCH_MILLISECONDS_MIN ? numeric : numeric * SECOND_MS)
            : Date.parse(raw);
        if (!Number.isFinite(timestamp)) return;

        const remaining = Math.max(0, timestamp - Date.now());
        const days = Math.floor(remaining / DAY_MS);
        const hours = Math.floor((remaining % DAY_MS) / HOUR_MS);
        const minutes = Math.ceil((remaining % HOUR_MS) / MINUTE_MS);
        element.textContent = remaining === 0
            ? '现在'
            : days ? `${days}d ${hours}h`
                : hours ? `${hours}h ${minutes}m`
                    : `${minutes}m`;
        element.title = new Date(timestamp).toLocaleString('zh-CN');
    });
}
