/* CPA shares account preparation with Sub2API; only its upload destination differs. */
(() => {
    const watchedJobs = new Set();
    const POLL_INTERVAL_MS = 2000;
    const BATCH_RESULT_TOAST_DURATION_MS = 8600;
    const IN_PROGRESS = new Set(['pending', 'running']);

    async function request(url, options = {}) {
        const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
        const result = await response.json();
        if (!response.ok || result.success === false) {
            throw new Error(result.error || result.detail || `CPA 请求失败（HTTP ${response.status}）`);
        }
        return result;
    }

    async function pushTeamToCpa(teamId, email) {
        if (!await confirmPageAction(`确定将 Team ${email || teamId} 的母号导出到 CPA 吗？`)) return;
        try {
            showToast('正在导出到 CPA...', 'info');
            const result = await request(`/admin/teams/${teamId}/push-cpa`, {method: 'POST'});
            showToast(`已导出到 CPA：${result.data.filename}`, 'success');
        } catch (error) {
            showToast(error.message, 'error');
        }
    }

    function batchPushTeamsToCpa() {
        return handleBatchAction('/admin/teams/batch-push-cpa', '批量导出到 CPA', {
            onSuccessToast(result) {
                const failures = result.results.filter(item => !item.success);
                const details = failures.map(item => `Team ${item.team_id}: ${item.error}`);
                return {
                    message: [result.message, ...details].join('。'),
                    type: failures.length ? 'warning' : 'success',
                    title: '批量 CPA 导出结果', duration: BATCH_RESULT_TOAST_DURATION_MS,
                };
            },
        });
    }

    async function pushPoolToCpa(button) {
        const {entryId, email, workspaceId} = button.dataset;
        if (!await confirmAccountPoolAction(`确定重新登录 ${email} 并导出到配置的 CPA 吗？`)) return;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            const result = await request(`/admin/account-pool/${entryId}/export-cpa`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({workspace_id: workspaceId || ''}),
            });
            showToast(`${email} 的 CPA 导出任务已开始`, 'success');
            watchJob(result.job_id, email);
            await refreshAccountPoolTable();
        } catch (error) {
            showToast(error.message, 'error');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }

    async function watchJob(jobId, email) {
        if (watchedJobs.has(jobId)) return;
        watchedJobs.add(jobId);
        try {
            while (true) {
                await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
                const job = await request(`/admin/account-pool/cpa-export-jobs/${jobId}`);
                if (IN_PROGRESS.has(job.status)) continue;
                if (job.status === 'failed') throw new Error(job.error || 'CPA 导出失败');
                if (job.status !== 'completed') throw new Error(`未知 CPA 导出状态：${job.status}`);
                showToast(`${email} 已导出到 CPA：${job.filename}`, 'success');
                break;
            }
        } catch (error) {
            showToast(error.message, 'error');
        } finally {
            // Keep this watcher registered during table refresh to prevent duplicate polling.
            try {
                await refreshAccountPoolTable();
            } catch (error) {
                showToast(`更新 CPA 导出状态失败：${error.message}`, 'error');
            }
            watchedJobs.delete(jobId);
        }
    }

    function initJobs() {
        document.querySelectorAll('[data-cpa-job-id]').forEach(label => {
            if (!IN_PROGRESS.has(label.dataset.status)) return;
            watchJob(Number(label.dataset.cpaJobId), label.closest('[data-account-pool-row]').dataset.email);
        });
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('.account-pool-cpa-button');
        if (button && !button.disabled) pushPoolToCpa(button);
    });
    window.pushTeamToCpa = pushTeamToCpa;
    window.batchPushTeamsToCpa = batchPushTeamsToCpa;
    window.initCpaExportJobs = initJobs;
})();
