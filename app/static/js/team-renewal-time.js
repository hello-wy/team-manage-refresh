/* Renewal dates carry an explicit offset so countdowns are independent of browser timezone. */
(() => {
    const MILLISECONDS_PER_MINUTE = 60_000;
    const MINUTES_PER_HOUR = 60;
    const HOURS_PER_DAY = 24;
    const MINUTES_PER_DAY = MINUTES_PER_HOUR * HOURS_PER_DAY;
    const REFRESH_INTERVAL_MS = 1_000;
    const TOGGLE_SELECTOR = '#teamTableRegion .team-renewal-toggle';
    const ACTIVE_SELECTOR = `${TOGGLE_SELECTOR}[aria-pressed="true"]`;
    let timer = null;

    function countdownLabel(value, now) {
        const deadline = Date.parse(value);
        if (!Number.isFinite(deadline)) throw new RangeError('续费时间无效');
        const remaining = deadline - now;
        if (remaining <= 0) return '已到续费时间';
        const totalMinutes = Math.ceil(remaining / MILLISECONDS_PER_MINUTE);
        const days = Math.floor(totalMinutes / MINUTES_PER_DAY);
        const hours = Math.floor(totalMinutes % MINUTES_PER_DAY / MINUTES_PER_HOUR);
        const minutes = totalMinutes % MINUTES_PER_HOUR;
        return `剩余 ${days} 天 ${hours} 小时 ${minutes} 分钟`;
    }

    function refreshCountdowns() {
        const buttons = document.querySelectorAll(ACTIVE_SELECTOR);
        if (!buttons.length) {
            clearInterval(timer);
            timer = null;
            return;
        }
        const now = Date.now();
        buttons.forEach(button => {
            button.textContent = countdownLabel(button.dataset.renewalAt, now);
        });
    }

    document.addEventListener('click', event => {
        const button = event.target.closest(TOGGLE_SELECTOR);
        if (!button) return;
        const showCountdown = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', String(showCountdown));
        button.textContent = button.dataset.renewalDate;
        button.title = showCountdown
            ? `续费时间：${button.dataset.renewalDate}（北京时间），点击显示日期`
            : '北京时间（UTC+8），点击查看倒计时';
        refreshCountdowns();
        if (showCountdown && timer === null) timer = setInterval(refreshCountdowns, REFRESH_INTERVAL_MS);
    });
})();
