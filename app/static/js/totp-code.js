(() => {
    const TICK_INTERVAL_MS = 200;
    const instances = new Set();
    let tickerId = null;

    function refreshInstances() {
        const timestamp = Date.now();
        instances.forEach(instance => instance.refresh(timestamp));
    }

    function startTicker() {
        if (tickerId !== null) return;
        tickerId = window.setInterval(refreshInstances, TICK_INTERVAL_MS);
    }

    function stopTicker() {
        if (instances.size || tickerId === null) return;
        window.clearInterval(tickerId);
        tickerId = null;
    }

    class TotpCode extends HTMLElement {
        constructor() {
            super();
            this._secret = '';
            this._code = '';
            this._step = null;
            this._generation = 0;
            this._render();
        }

        connectedCallback() {
            instances.add(this);
            startTicker();
            this.refresh(Date.now());
            this._loadFromEmail();
        }

        disconnectedCallback() {
            instances.delete(this);
            stopTicker();
        }

        set secret(value) {
            this.setSecret(value);
        }

        get secret() {
            return this._secret;
        }

        setSecret(value) {
            this._generation += 1;
            this._secret = window.totp.normalizeSecret(value);
            this._code = '';
            this._step = null;
            if (!this._secret) {
                this._setState('empty', '未保存');
                return;
            }
            this._setState('loading', '计算中');
            this.refresh(Date.now());
        }

        showLoading(message = '读取中') {
            this._clearValue();
            this._setState('loading', message);
        }

        showError(message = '读取失败') {
            this._clearValue();
            this._setState('error', message);
        }

        reset() {
            this._clearValue();
            this._setState('idle', '等待读取');
        }

        refresh(timestamp) {
            const timeWindow = window.totp.getWindow(timestamp);
            this.style.setProperty('--totp-progress', timeWindow.progress);
            this._seconds.textContent = `${timeWindow.remainingSeconds}s`;
            this.setAttribute('aria-label', `2FA 动态验证码，剩余 ${timeWindow.remainingSeconds} 秒`);
            if (this._secret && this._step !== timeWindow.step) {
                this._generate(timestamp, timeWindow.step);
            }
        }

        async copy() {
            try {
                if (!this._code && this._secret) await this._generate(Date.now());
                if (!this._code) throw new Error('当前没有可复制的验证码');
                await window.copyToClipboard(this._code);
                this._setCopied();
            } catch (error) {
                window.showToast(error.message || '复制验证码失败', 'error');
            }
        }

        async _loadFromEmail() {
            const email = this.dataset.email;
            if (!email) return;
            this._setState('loading', '读取中');
            try {
                const credentials = await window.accountPoolCredentialStore.load(email);
                if (this.isConnected) this.setSecret(credentials.two_factor_secret);
            } catch (error) {
                if (this.isConnected) this._setState('error', error.message || '读取失败');
            }
        }

        async _generate(timestamp, step = window.totp.getWindow(timestamp).step) {
            const generation = ++this._generation;
            this._step = step;
            try {
                const code = await window.totp.generate(this._secret, {timestamp});
                if (generation !== this._generation || !this.isConnected) return;
                this._code = code;
                this._setState('ready', `${code.slice(0, 3)} ${code.slice(3)}`);
            } catch (error) {
                if (generation !== this._generation || !this.isConnected) return;
                this._code = '';
                this._setState('error', error.message || '计算失败');
            }
        }

        _render() {
            this.classList.add('totp-code');
            this.innerHTML = `
                <div class="totp-code-frame">
                    <button type="button" class="totp-code-value" title="复制当前验证码">
                        <span class="totp-code-digits">等待读取</span>
                        <span class="totp-code-seconds" aria-hidden="true">30s</span>
                    </button>
                    <button type="button" class="totp-code-copy" title="复制当前验证码" aria-label="复制当前 2FA 验证码">
                        <i data-lucide="copy" aria-hidden="true"></i>
                    </button>
                </div>`;
            this._digits = this.querySelector('.totp-code-digits');
            this._seconds = this.querySelector('.totp-code-seconds');
            this._copyButton = this.querySelector('.totp-code-copy');
            this.querySelector('.totp-code-value').addEventListener('click', () => this.copy());
            this._copyButton.addEventListener('click', () => this.copy());
        }

        _clearValue() {
            this._generation += 1;
            this._secret = '';
            this._code = '';
            this._step = null;
        }

        _setState(state, message) {
            this.dataset.state = state;
            this._digits.textContent = message;
            this._copyButton.disabled = state !== 'ready';
        }

        _setCopied() {
            this.dataset.copied = 'true';
            window.setTimeout(() => delete this.dataset.copied, 900);
        }
    }

    window.customElements.define('totp-code', TotpCode);
})();
