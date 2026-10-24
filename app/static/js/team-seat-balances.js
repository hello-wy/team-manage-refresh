/* Hydrate list balances without opening or modifying the member-management dialog. */
(() => {
    const queue = [];
    let active = 0;
    const concurrency = 2;

    function showFailure(row) {
        // Keep the last confirmed balance when a refresh fails.
        row.querySelector('.team-seat-hint')?.remove();
        row.querySelector('.team-seat-retry')?.remove();
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'team-seat-retry';
        retry.textContent = '余额读取失败，点击重试';
        row.appendChild(retry);
    }

    async function load(row) {
        try {
            const result = await apiCall(`/admin/teams/${Number(row.dataset.teamId)}/seats/balance`, {
                method: 'POST', cache: 'no-store',
            });
            if (!row.isConnected) return;
            if (!result.success || !result.data?.success) throw new Error('余额读取失败');
            // A member sync may have replaced this data while the request was in flight.
            const current = row.dataset.seatBalanceObservedAt;
            const observedAt = result.data.observed_at;
            if (!current || !observedAt || observedAt >= current) {
                updateTeamMemberCount(Number(row.dataset.teamId), result.data);
                row.dataset.seatBalanceObservedAt = observedAt || '';
            }
            row.dataset.seatBalanceState = 'done';
            row.querySelector('.team-seat-retry')?.remove();
        } catch (_) {
            if (!row.isConnected || row.dataset.seatBalanceState === 'done') return;
            row.dataset.seatBalanceState = 'failed';
            showFailure(row);
        }
    }

    function drain() {
        while (active < concurrency && queue.length) {
            const row = queue.shift();
            if (!row.isConnected || row.dataset.seatBalanceState === 'done') continue;
            active++;
            void load(row).finally(() => { active--; drain(); });
        }
    }

    function refresh() {
        document.querySelectorAll('#teamTableRegion .team-seat-usage[data-seat-balance-state="pending"]').forEach(row => {
            row.dataset.seatBalanceState = 'queued';
            queue.push(row);
        });
        drain();
    }

    document.addEventListener('click', event => {
        const retry = event.target.closest('#teamTableRegion .team-seat-retry');
        if (!retry) return;
        const row = retry.closest('.team-seat-usage');
        retry.remove();
        row.dataset.seatBalanceState = 'pending';
        refresh();
    });
    document.addEventListener('DOMContentLoaded', refresh);
    window.teamSeatBalances = {refresh};
})();
